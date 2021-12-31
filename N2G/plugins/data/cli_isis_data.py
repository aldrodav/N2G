"""
CLI ISIS LSDB Data Plugin
*************************

This module designed to process network devices CLI output
of ISIS LSDB content. That output parsed with TTP Templates 
and processed further to populate N2G Drawing with nodes and 
links details.

Dependencies:

* TTP >= 0.8.0, to install: ``pip install ttp``
* TTP Templates >= 0.2.0, to install: ``pip install ttp-templates``

If no TTP modules installed, on an attempt to instantiate ``cli_isis_data``
object ``ModuleNotFoundError`` exception raised.

**Feature Support matrix**

+---------------+------------+
| Platform      | ISIS       |
| Name          | LSDB       |
+===============+============+
| cisco_ios     |     ---    |
+---------------+------------+
| cisco_xr      |     YES    |
+---------------+------------+
| cisco_nxos    |     ---    |
+---------------+------------+
| huawei        |     ---    |
+---------------+------------+

**Commands output required**

+---------------+-----------------------------+
| Platform      | Commands                    |
| Name          |                             |
+===============+=============================+
| cisco_ios     |     ---                     |
+---------------+-----------------------------+
| cisco_xr      | show isis database verbose  |
+---------------+-----------------------------+
| cisco_nxos    |     ---                     |
+---------------+-----------------------------+
| huawei        |     ---                     |
+---------------+-----------------------------+

``*`` - primary command, other commands are optional

How it works
------------

Output from devices parsd using TTP Templates into a dictionary structure, reference
``ttp://misc/N2G/N2G/isis_lsdb/`` templates for parsing templates content and samples
of structure produced.

After parsing, results processed further to form a dictionary of nodes and links keyed 
by unique nodes and links identifiers, dictionary values are nodes dictionaries and for links
it is a list of dictionaries of links between pair of nodes. For nodes ISIS RID 
used as a unique ID, for links it is sorted tuple of ``source``, ``target`` and ``label`` 
keys' values. This structure helps to eliminate duplicates.

Next step is post processing, such as packing links between nodes or IP lookups.

Last step is to populate N2G drawing with new nodes and links using ``from_dict`` method.

Sample usage
------------

TBD

API Reference
-------------

.. autoclass:: N2G.plugins.data.cli_isis_data.cli_isis_data
   :members:
"""
import logging
import json
import os
import csv
import ipaddress
from fnmatch import fnmatchcase

try:
    from ttp import ttp
    from ttp_templates import get_template

    HAS_TTP = True
except ImportError:
    HAS_TTP = False

# initiate logging
log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Main class:
# -----------------------------------------------------------------------------


class cli_isis_data:
    """
    Main class to instantiate ISIS LSDB drawer object.

    :param drawing: (obj) N2G Diagram object
    :param ttp_vars: (dict) Dictionary to use as vars attribute while instantiating
      TTP parser object
    :param ip_lookup_data: (dict or str) IP Lookup dictionary or OS path to CSV file
    :param add_connected: (bool) if True, will add connected subnets as nodes, default is False    
    :param ptp_filter: (list) list of glob patterns to filter point-to-point links based on link IP
    :param add_data: (bool) if True (default) adds data information to nodes and links
    
    ``ip_lookup_data`` dictionary must be keyed by ISSI RID IP address, with values
    being dictionary which must contain ``hostname`` key with optional additional keys 
    to use for N2G diagram module node, e.g. ``label``, ``top_label``, ``bottom_label``, 
    ``interface``etc. If ``ip_lookup_data`` is an OS path to CSV file, that file's first 
    column header must be ``ip`` , file must contain ``hostname`` column, other columns 
    values set to N2G diagram module node attributes, e.g. ``label``, ``top_label``, 
    ``bottom_label``, ``interface`` etc. 
    
    If lookup data contains ``interface`` key, it will be added to link label.
        
    Sample ip_lookup_data dictionary::
    
        {
            "1.1.1.1": {
                "hostname": "router-1",
                "bottom_label": "1 St address, City X",
                "interface": "Gi1"
            }
        }

    Sample ip_lookup_data CSV file::

        ip,hostname,bottom_label,interface
        1.1.1.1,router-1,"1 St address, City X",Gi1
    """

    def __init__(
        self,
        drawing,
        ttp_vars: dict = {},
        ip_lookup_data: dict = {},
        add_connected: bool = False,
        ptp_filter: list = [],
        add_data: bool = True,
    ):
        self.ttp_vars = ttp_vars
        self.drawing = drawing
        self.drawing.node_duplicates = "update"
        self.add_connected = add_connected
        self.ptp_filter = ptp_filter
        self.add_data = add_data
        self.parsed_data = {}
        self.nodes_dict = {}
        self.links_dict = {}
        self.graph_dict = {"nodes": [], "links": []}
        self.ip_lookup_data = ip_lookup_data
        self._load_ip_lookup_data()

    def _load_ip_lookup_data(self) -> None:
        """
        Helper function to load CSV table in a dictionary keyed
        by values in first column. This dictionary further used
        to perform IP to node details lookup for ISIS router ID.
        """
        # load lookup data
        if self.ip_lookup_data and isinstance(self.ip_lookup_data, str):
            with open(self.ip_lookup_data) as f:
                reader = csv.DictReader(f)
                self.ip_lookup_data = {r["ip"]: r for r in reader if "ip" in r}

    def work(self, data):
        """
        Method to parse text data and add nodes and links to N2G drawing.

        :param data: (dict or str) dictionary keyed by platform name or OS path
          string to directories with text files

        If data is dictionary, keys must correspond to "Platform" column in
        *Supported platforms* table, values are lists of text items to
        process.

        Data dictionary sample::

            data = {
                "cisco_ios" : ["h1", "h2"],
                "cisco_ios-XR": ["h3", "h4"],
                "cisco_nxos": ["h5", "h6"],
                ...etc...
            }

        Where ``hX`` device's show commands output.

        If data is a string with OS path to directory, sub directories names
        must correspond to "Platform" column in *Supported platforms* table.
        Each child directory should contain text files with show commands output
        for each device.

        Directories structure sample::

            data = "/path/to/data/"

            /path/to/data/
                         |__/cisco_ios/<text files>
                         |__/cisco_xr/<text files>
                         |__/huawei/<text files>
                         |__/...etc...
        """
        self._parse(data)
        self._form_base_graph_dict()
        self._pack_links()
        if self.ip_lookup_data:
            self._lookup_rid()
            self._lookup_ip_interfaces()
        self._update_drawing()

    def _make_hash_tuple(self, data: dict) -> tuple:
        """
        Helper function to form Edge tuple to use as a hash to
        identify all the links between same pair of nodes using
        ``source``, ``target`` and ``label`` keys' values.

        :param data: (dict) link dictionary with source, target and label keys
        """
        return tuple(sorted([data["source"], data["target"], data.get("label", "")]))

    def _parse(self, data: [dict, str]) -> None:
        """
        Method to parse data using TTP Templates

        :param data: (dict or str) dictionary of data items or OS
            path to folders with data to parse
        :return: None
        """
        if not HAS_TTP:
            raise ModuleNotFoundError(
                "N2G:cli_isis_data failed importing TTP, is it installed?"
            )
        parser = ttp(vars=self.ttp_vars, log_level="ERROR")
        # process data dictionary
        if isinstance(data, dict):
            for platform_name, text_list in data.items():
                ttp_template = get_template(
                    misc="N2G/cli_isis_data/{}.txt".format(platform_name)
                )
                parser.add_template(template=ttp_template, template_name=platform_name)
                for item in text_list:
                    parser.add_input(item, template_name=platform_name)
        # process directories at OS path
        elif isinstance(data, str):
            # get all sub-folders and load respective templates
            with os.scandir(data) as dirs:
                for entry in dirs:
                    if entry.is_dir():
                        platform_name = entry.name
                        ttp_template = get_template(
                            misc="N2G/cli_isis_data/{}.txt".format(platform_name)
                        )
                        parser.add_template(
                            template=ttp_template, template_name=platform_name
                        )
                        parser.add_input(
                            data=os.path.abspath(entry), template_name=platform_name
                        )
        else:
            raise TypeError(
                "Expecting dictionary or string, but '{}' given".format(type(data))
            )
        parser.parse(one=True)
        self.parsed_data = parser.result(structure="flat_list")
        # import pprint; pprint.pprint(self.parsed_data, width = 100)

    def _process_lsp(self, lsp: dict, isis_pid: str, device: dict) -> None:
        """"""
        # make node out of router LSP
        self._add_node(
            node={
                "id": lsp["hostname"],
                "label": lsp["hostname"],
                "bottom_label": "Node",
                "top_label": lsp["rid"],
            },
            node_data=lsp,
        )
        # go over links
        for link in lsp.get("links", []):
            # ignore ISIS links based on IP addresses
            if any([fnmatchcase(link.get("local_ip", ""), p) for p in self.ptp_filter]):
                continue
            self._add_link(
                link={
                    "source": lsp["hostname"],
                    "src_label": "{}:{}".format(
                        link.get("local_ip", link.get("local_intf_id")), link["metric"]
                    ),
                    "label": "{}:{}".format(
                        isis_pid, lsp["level"].replace("Level-", "L")
                    ),
                    "target": link["peer_name"],
                    "local_intf_id": link.get("local_intf_id"),
                    "peer_intf_id": link.get("peer_intf_id"),
                    "peer_ip": link.get("peer_ip"),
                    "local_ip": link.get("local_ip"),
                },
                link_data={lsp["hostname"]: {"isis_pid": isis_pid, **link}},
            )
        # go over connected subnets
        if self.add_connected:
            for network in lsp.get("networks", []):
                self._add_node(
                    node={
                        "id": network["network"],
                        "label": network["network"],
                        "bottom_label": "Subnet",
                    }
                )
                self._add_link(
                    link={
                        "source": lsp["hostname"],
                        "src_label": "M:{}".format(network["metric"]),
                        "label": lsp["level"],
                        "target": network["network"],
                    }
                )

    def _form_base_graph_dict(self) -> None:
        for device in self.parsed_data:
            # go over all ISIS processes on the box
            for isis_pid, isis_data in device.get("isis_processes", {}).items():
                # process LSP
                for lsp in isis_data.get("LSP", []):
                    self._process_lsp(lsp, isis_pid, device)

    def _add_node(self, node: dict, node_data: dict = {}) -> None:
        # add new node
        if not node["id"] in self.nodes_dict:
            if node_data and self.add_data:
                node["description"] = json.dumps(
                    node_data, sort_keys=True, indent=4, separators=(",", ": ")
                )
            self.nodes_dict[node["id"]] = node
        # update node attributes if they do not exists already
        else:
            stored_node = self.nodes_dict[node["id"]]
            for key, value in node.items():
                if not key in stored_node:
                    stored_node[key] = value
            if not "description" in stored_node and node_data and self.add_data:
                stored_node["description"] = json.dumps(
                    node_data, sort_keys=True, indent=4, separators=(",", ": ")
                )

    def _add_link(self, link: dict, link_data: dict = {}) -> None:
        link_hash = self._make_hash_tuple(link)
        self.links_dict.setdefault(link_hash, [])
        if link not in self.links_dict[link_hash]:
            if link_data and self.add_data:
                link["description"] = json.dumps(
                    link_data, sort_keys=True, indent=4, separators=(",", ": ")
                )
            self.links_dict[link_hash].append(link)

    def _pack_links(self) -> None:
        """
        Method to iterate over links between node pairs and pack links based
        on the local and peer interface IDs
        """
        for hash in self.links_dict.keys():
            links = self.links_dict[hash]
            # continue if only one link between node pairs
            if len(links) <= 1:
                continue
            self.links_dict[hash] = []
            while links:
                link = links.pop()
                pair_link_index = None
                local_intf_id = link.pop("local_intf_id")
                peer_intf_id = link.pop("peer_intf_id")
                peer_ip = link.pop("peer_ip")
                local_ip = link.pop("local_ip")
                # try to find link pair using ISIS interface ID
                if local_intf_id and peer_intf_id:
                    for link_2 in links:
                        if (
                            link_2["peer_intf_id"] == local_intf_id
                            and link_2["local_intf_id"] == peer_intf_id
                        ):
                            link["trgt_label"] = link_2["src_label"]
                            self._add_link(
                                link=link,
                                link_data={
                                    **json.loads(link["description"]),
                                    **json.loads(link_2["description"]),
                                },
                            )
                            # form new link label if PID does not match
                            if link["label"] != link_2["label"]:
                                pid_1, level_1 = link["label"].split(":")
                                pid_2, level_2 = link_2["label"].split(":")
                                link["label"] = "{}:{}".format(
                                    pid_1
                                    if pid_1 == pid_2
                                    else "{}-{}".format(pid_1, pid_2),
                                    level_1,
                                )
                            pair_link_index = links.index(link_2)
                            break
                # try to find link pair using interface IP addresses
                elif local_ip and peer_ip:
                    for link_2 in links:
                        if (
                            link_2["peer_ip"] == local_ip
                            and link_2["local_ip"] == peer_ip
                        ):
                            link["trgt_label"] = link_2["src_label"]
                            self._add_link(
                                link=link,
                                link_data={
                                    **json.loads(link["description"]),
                                    **json.loads(link_2["description"]),
                                },
                            )
                            # form new link label if PID does not match
                            if link["label"] != link_2["label"]:
                                pid_1, level_1 = link["label"].split(":")
                                pid_2, level_2 = link_2["label"].split(":")
                                link["label"] = "{}:{}".format(
                                    pid_1
                                    if pid_1 == pid_2
                                    else "{}-{}".format(pid_1, pid_2),
                                    level_1,
                                )
                            pair_link_index = links.index(link_2)
                            break
                # remove link from links if it was combined
                if pair_link_index is not None:
                    _ = links.pop(pair_link_index)
                # add link back to links if have not found a match for it
                else:
                    self._add_link(link)

    def _lookup_rid(self):
        """
        Method to lookup RID in lookup data and update node labels.
        """
        for node in self.nodes_dict.values():
            node_ip = node["top_label"]
            if node_ip in self.ip_lookup_data:
                node.update(
                    {
                        k: v
                        for k, v in self.ip_lookup_data[node_ip].items()
                        if k in ["top_label", "bottom_label", "label"]
                    }
                )

    def _lookup_ip_interfaces(self):
        """
        Method to search for link IP addresses in lookup table and add 
        interface names to the link labels.
        """
        for links in self.links_dict.values():
            for link in links:
                # modify source label
                if link.get("src_label"):
                    link_src_ip, link_src_metric = link["src_label"].split(":")
                    # skip link_src_ip if its not IPv4 address
                    if link_src_ip.count(".") != 3:
                        continue
                    if self.ip_lookup_data.get(link_src_ip, {}).get("interface"):
                        link["src_label"] = "{}:{}:{}".format(
                            self.ip_lookup_data[link_src_ip]["interface"],
                            link_src_ip,
                            link_src_metric,
                        )
                # modify target label
                if link.get("trgt_label"):
                    link_trgt_ip, link_trgt_metric = link["trgt_label"].split(":")
                    # skip link_trgt_ip if its not IPv4 address
                    if link_trgt_ip.count(".") != 3:
                        continue
                    if self.ip_lookup_data.get(link_trgt_ip, {}).get("interface"):
                        link["trgt_label"] = "{}:{}:{}".format(
                            self.ip_lookup_data[link_trgt_ip]["interface"],
                            link_trgt_ip,
                            link_trgt_metric,
                        )

    def _update_drawing(self):
        """
        Method to add formed links and nodes to the drawing object
        """
        self.graph_dict["nodes"] = list(self.nodes_dict.values())
        for i in self.links_dict.values():
            self.graph_dict["links"].extend(i)
        # import pprint; pprint.pprint(self.graph_dict, width =100)
        self.drawing.from_dict(self.graph_dict)