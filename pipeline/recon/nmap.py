import ast
import pickle
import logging
import subprocess
import concurrent.futures
from pathlib import Path

import luigi
from sqlalchemy import or_
from luigi.util import inherits

from .masscan import ParseMasscanOutput
from .config import defaults, tool_paths
from ..luigi_targets import SQLiteTarget
from ..models import DBManager, NmapResult, Target, IPAddress, SearchsploitResult


@inherits(ParseMasscanOutput)
class ThreadedNmapScan(luigi.Task):
    """ Run ``nmap`` against specific targets and ports gained from the ParseMasscanOutput Task.

    Install:
        ``nmap`` is already on your system if you're using kali.  If you're not using kali, refer to your own
        distributions instructions for installing ``nmap``.

    Basic Example:
        .. code-block:: console

            nmap --open -sT -sC -T 4 -sV -Pn -p 43,25,21,53,22 -oA htb-targets-nmap-results/nmap.10.10.10.155-tcp 10.10.10.155

    Luigi Example:
        .. code-block:: console

            PYTHONPATH=$(pwd) luigi --local-scheduler --module recon.nmap ThreadedNmap --target-file htb-targets --top-ports 5000

    Args:
        threads: number of threads for parallel nmap command execution
        db_location: specifies the path to the database used for storing results *Required by upstream Task*
        rate: desired rate for transmitting packets (packets per second) *Required by upstream Task*
        interface: use the named raw network interface, such as "eth0" *Required by upstream Task*
        top_ports: Scan top N most popular ports *Required by upstream Task*
        ports: specifies the port(s) to be scanned *Required by upstream Task*
        target_file: specifies the file on disk containing a list of ips or domains *Required by upstream Task*
        results_dir: specifes the directory on disk to which all Task results are written *Required by upstream Task*
    """

    threads = luigi.Parameter(default=defaults.get("threads", ""))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_mgr = DBManager(db_location=self.db_location)
        self.highest_id = self.db_mgr.get_highest_id(table=NmapResult)

    def requires(self):
        """ ThreadedNmap depends on ParseMasscanOutput to run.

        TargetList expects target_file, results_dir, and db_location as parameters.
        Masscan expects rate, target_file, interface, and either ports or top_ports as parameters.

        Returns:
            luigi.Task - ParseMasscanOutput
        """
        args = {
            "results_dir": self.results_dir,
            "rate": self.rate,
            "target_file": self.target_file,
            "top_ports": self.top_ports,
            "interface": self.interface,
            "ports": self.ports,
            "db_location": self.db_location,
        }
        return ParseMasscanOutput(**args)

    def output(self):
        """ Returns the target output for this task.

        Naming convention for the output folder is TARGET_FILE-nmap-results.

        The output folder will be populated with all of the output files generated by
        any nmap commands run.  Because the nmap command uses -oA, there will be three
        files per target scanned: .xml, .nmap, .gnmap.

        Returns:
            luigi.local_target.LocalTarget
        """
        # TODO: remove file based completion
        # return SQLiteTarget(table=NmapResult, db_location=self.db_location, index=self.highest_id)

        results_subfolder = Path(self.results_dir) / "nmap-results"

        return luigi.LocalTarget(results_subfolder.resolve())

    def parse_nmap_output(self):
        """ Read nmap .nmap results and add entries into specified database. """
        sentry = False

        for entry in Path(self.output().path).glob("nmap*.nmap"):
            text = list()

            # nmap.10.10.10.10-tcp.nmap -> 10.10.10.10
            ipaddr = entry.stem.replace("nmap.", "").split("-")[0]

            for line in entry.read_text().splitlines():

                if sentry:
                    text.append(line)

                if "PORT" in line and "STATE" in line and "SERVICE" in line:
                    # found header (PORT    STATE SERVICE  VERSION)
                    sentry = True
                elif "Service detection performed" in line:
                    # found epilog
                    text.pop()  # get rid of the line we just found, as it's already appended
                    sentry = False

            nmr = NmapResult(text="\n".join(text))

            tgt = (
                self.db_mgr.session.query(Target)
                .filter(or_(IPAddress.ipv4_address == ipaddr, IPAddress.ipv6_address == ipaddr))
                .first()
            )

            tgt.nmap_results.append(nmr)

            self.db_mgr.add(tgt)
        self.db_mgr.close()

    def run(self):
        """ Parses pickled target info dictionary and runs targeted nmap scans against only open ports. """
        try:
            self.threads = abs(int(self.threads))
        except TypeError:
            return logging.error("The value supplied to --threads must be a non-negative integer.")

        ip_dict = pickle.load(open(self.input().path, "rb"))

        nmap_command = [  # placeholders will be overwritten with appropriate info in loop below
            "nmap",
            "--open",
            "PLACEHOLDER-IDX-2",
            "-n",
            "-sC",
            "-T",
            "4",
            "-sV",
            "-Pn",
            "-p",
            "PLACEHOLDER-IDX-10",
            "-oA",
        ]

        commands = list()

        """
        ip_dict structure
        {
            "IP_ADDRESS":
                {'udp': {"161", "5000", ... },
                ...
                i.e. {protocol: set(ports) }
        }
        """
        for target, protocol_dict in ip_dict.items():
            for protocol, ports in protocol_dict.items():

                tmp_cmd = nmap_command[:]
                tmp_cmd[2] = "-sT" if protocol == "tcp" else "-sU"

                # arg to -oA, will drop into subdir off curdir
                tmp_cmd[10] = ",".join(ports)
                tmp_cmd.append(str(Path(self.output().path) / f"nmap.{target}-{protocol}"))

                tmp_cmd.append(target)  # target as final arg to nmap

                commands.append(tmp_cmd)

        # basically mkdir -p, won't error out if already there
        Path(self.output().path).mkdir(parents=True, exist_ok=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.threads) as executor:

            executor.map(subprocess.run, commands)

        self.parse_nmap_output()


@inherits(ThreadedNmapScan)
class SearchsploitScan(luigi.Task):
    """ Run ``searchcploit`` against each ``nmap*.xml`` file in the **TARGET-nmap-results** directory and write results to disk.

    Install:
        ``searchcploit`` is already on your system if you're using kali.  If you're not using kali, refer to your own
        distributions instructions for installing ``searchcploit``.

    Basic Example:
        .. code-block:: console

            searchsploit --nmap htb-targets-nmap-results/nmap.10.10.10.155-tcp.xml

    Luigi Example:
        .. code-block:: console

            PYTHONPATH=$(pwd) luigi --local-scheduler --module recon.nmap Searchsploit --target-file htb-targets --top-ports 5000

    Args:
        threads: number of threads for parallel nmap command execution *Required by upstream Task*
        db_location: specifies the path to the database used for storing results *Required by upstream Task*
        rate: desired rate for transmitting packets (packets per second) *Required by upstream Task*
        interface: use the named raw network interface, such as "eth0" *Required by upstream Task*
        top_ports: Scan top N most popular ports *Required by upstream Task*
        ports: specifies the port(s) to be scanned *Required by upstream Task*
        target_file: specifies the file on disk containing a list of ips or domains *Required by upstream Task*
        results_dir: specifies the directory on disk to which all Task results are written *Required by upstream Task*
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db_mgr = DBManager(db_location=self.db_location)
        self.highest_id = self.db_mgr.get_highest_id(table=NmapResult)

    def requires(self):
        """ Searchsploit depends on ThreadedNmap to run.

        TargetList expects target_file, results_dir, and db_location as parameters.
        Masscan expects rate, target_file, interface, and either ports or top_ports as parameters.
        ThreadedNmap expects threads

        Returns:
            luigi.Task - ThreadedNmap
        """
        args = {
            "rate": self.rate,
            "ports": self.ports,
            "threads": self.threads,
            "top_ports": self.top_ports,
            "interface": self.interface,
            "target_file": self.target_file,
            "results_dir": self.results_dir,
            "db_location": self.db_location,
        }
        return ThreadedNmapScan(**args)

    def output(self):
        """ Returns the target output for this task.

        Naming convention for the output folder is TARGET_FILE-searchsploit-results.

        The output folder will be populated with all of the output files generated by
        any searchsploit commands run.

        Returns:
            luigi.local_target.LocalTarget
        """
        # results_subfolder = Path(self.results_dir) / "searchsploit-results"
        #
        # return luigi.LocalTarget(results_subfolder.resolve())
        return SQLiteTarget(table=SearchsploitResult, db_location=self.db_location, index=self.highest_id)

    def run(self):
        """ Grabs the xml files created by ThreadedNmap and runs searchsploit --nmap on each one, saving the output. """
        results = dict()

        for entry in Path(self.input().path).glob("nmap*.xml"):
            proc = subprocess.run(
                [tool_paths.get("searchsploit"), "-j", "-v", "--nmap", str(entry)], stdout=subprocess.PIPE
            )
            if proc.stdout:
                # Path(self.output().path).mkdir(parents=True, exist_ok=True)
                #
                # change  wall-searchsploit-results/nmap.10.10.10.157-tcp to 10.10.10.157
                ipaddr = entry.stem.replace("nmap.", "").replace("-tcp", "").replace("-udp", "")
                #
                # Path(f"{self.output().path}/searchsploit.{target}-{entry.stem[-3:]}.txt").write_bytes(proc.stderr)
                contents = proc.stdout.decode()
                for line in contents.splitlines():
                    if "Title" in line:
                        # {'Title': "Nginx (Debian Based Distros + Gentoo) ... }
                        if line.endswith(","):
                            # result would be a tuple if the comma is left on the line; remove it
                            tmp_result = ast.literal_eval(line.strip()[:-1])
                        else:
                            # normal dict
                            tmp_result = ast.literal_eval(line.strip())

                        tgt = (
                            self.db_mgr.session.query(Target)
                            .filter(or_(IPAddress.ipv4_address == ipaddr, IPAddress.ipv6_address == ipaddr))
                            .first()
                        )
                        ssr_type = tmp_result.get("Type")
                        ssr_title = tmp_result.get("Title")
                        ssr_path = tmp_result.get("Path")

                        ssr = SearchsploitResult(type=ssr_type, title=ssr_title, path=ssr_path)

                        tgt.searchsploit_results.append(ssr)

                        self.db_mgr.add(tgt)

        self.db_mgr.close()
