#
# Script that
#  1) reads yaml config gile
#  2) fetches CED elements defined in config file
#  3) fetches Mya data for CED elements as specified in config file
#  4) fetches global Mya data
#  5) - for each interval specified in config file that passes filters:
#  5)   - Output nodes in HBG file format
#  6)   - Build edges and output in HBG format

import shutil
import yaml
import argparse
import os
import sys
import logging
import datetime
import pytz
from modules.ced import *
import modules.ced as ced
import modules.mya as mya
import modules.hgb as hgb
import modules.node as node
from modules.util import progressBar
from data_loader.data_loader import CEBAFGraphLoader

# Suppress the warnings we know will be generated by having to
# bypass SSL verification because of the annoying JLAB MITM tampering
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# The top-level directory where data will be written
output_dir = None

# The file names that will be used when saving the data fetched from
# CED and MYA as json and when reading that data back in lieu of
# accessing those services.  The primary purpose of these files
# is for development/testing/debugging
tree_file = 'tree.json'
nodes_file = 'nodes.json'
globals_file = 'global.json'

# the list of nodes that will be used to output graph data
node_list = []

# the global data that will be used for filtering
global_data = []

# CED Type hierarchy tree for using to match specific retrieved types
# to the possibly more generic (i.e. parent) type names encountered in the config dictionary.
# For example to determine that an element whose type is QB is also a "Quad" and a "Magnet"
tree = TypeTree()


# Define the program's command line arguments and build a parser to process them
def make_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Command Line Options')
    parser.add_argument("-c", type=str, dest='config_file', default="config.yaml",
                        help="Name of a yaml formatted config file")
    parser.add_argument("-d", type=str, dest='output_dir', default='.',
                        help="Directory where generated graph file hierarchy will be written")
    parser.add_argument("--read-json", action='store_true',
                        help=f"Read data from {tree_file}, {nodes_file}, and {globals_file} instead of CED and Mya")
    parser.add_argument("--save-json", action='store_true',
                        help=f"Save fetched data in {tree_file}, {nodes_file}, and {globals_file}")

    return parser


# Initialize module-level configurations
def initialize_modules(config: dict):
    # Class attributes of the ced module
    if 'history' in config['ced']:
        ced.history = True
    if 'workspace' in config['ced']:
        ced.workspace = config['ced']['workspace']

    # Class attributes of the mya module
    if 'deployment' in config['mya']:
        mya.deployment = config['mya']['deployment']
    if 'throttle' in config['mya']:
        mya.throttle = config['mya']['throttle']

    # Class attributes of the node module
    node.default_attributes = config['nodes']['default_attributes']

if __name__ == "__main__":
    try:
        # Access the command line arguments
        args = make_cli_parser().parse_args()

        # If defaulting to '.' try to make a subdir
        if args.output_dir == '.':
            output_dir = hgb.dir_from_date('.', datetime.datetime.now(pytz.timezone('America/New_York')))
            os.makedirs(output_dir)
        else:
            output_dir = args.output_dir

        # Before doing any time-consuming work, verify the output dir is writable
        if not os.access(output_dir, os.X_OK | os.W_OK):
            sys.exit('Unable to write to output directory ' + output_dir)
        else:
            print("Output will be written to " + output_dir)

        logging.basicConfig(
            level=logging.INFO,
            filename='warnings.log',
            filemode='w'  # Fresh file every run.
        )

        # Read configuration yaml file
        stream = open(args.config_file, 'r')
        config = yaml.load(stream, Loader=yaml.CLoader)

        # Module-level configuration
        initialize_modules(config)

        # The conditional block below chooses between two methods of populating the node list
        # 1) Reading saved data or
        # 2) Going out to CED and MYA to get fresh data
        if args.read_json:
            # Read the type tree file
            with open(tree_file, 'r') as tree_file_handle:
                data = tree_file_handle.read()
            tree.tree = json.loads(data)  # pre-populate the data so no need to lazy-load later
            # Read the global data
            with open(globals_file, 'r') as globals_file_handle:
                data = globals_file_handle.read()
            global_data = json.loads(data)
            # And finally the node list
            node_list = node.List.from_json(nodes_file, tree_file, args.config_file)
        else:

            # Use CED and MYA to build nodes list
            # Begin by fetching the desired CED elements
            # TODO - feedback to user b/c this can also take a while
            inventory = Inventory(
                config['ced']['zone'],
                config['ced']['types'],
                config['ced']['properties'],
                config['ced']['expressions']
            )
            elements = inventory.elements()

            # The dates for fetching
            dates = mya.date_ranges(config)

            # Retrieve the global PV list
            sys.stdout.write("Fetching Global Data: ")
            global_data = mya.Sampler(dates, config['mya']['global']).data(with_spin=True)
            sys.stdout.write("\n")

            # It's important to preserve the order of the elements in the nodeList.
            # We are going to assign each node a node_id property that corresponds to its
            # order in the list beginning at 0.
            node_id = 0
            for element in progressBar(elements, prefix='Fetching Node Data:', suffix='', length=60):
                # Wrap node creating in a try-catch block so that we can simply log problematic nodes
                # without killing the entire effort.
                try:
                    item = node.List.make_node(element, tree, config, dates)

                    # If no node was created, it means that there was not type match.  This could happen if
                    # the CED query was something broad like "BeamElem", but the config file only indicates the
                    # desired EPICS fields for specific sub-types (Magnet, BPM, etc.)
                    if item:
                        # Load the data now so that we can give user a progressbar
                        item.pv_data()
                        # Assign id values based on order of encounter
                        item.node_id = node_id
                        node_list.append(item)
                        node_id += 1
                except mya.MyaException as err:
                    print(err)
            # Link each SetPointNode to its downstream nodes up to and including the next SetPoint.
            node.List.populate_links(node_list)

        # Throw an exception if we have an empty node_list at this point to guard against having been provided
        # empty date ranges
        if len(node_list) < 1:
            raise RuntimeError("Empty node list.  Did you provide valid dates?")

        # Link each SetPointNode to its downstream nodes up to and including the next SetPoint.
        node.List.populate_links(node_list)

        # At this point we've got all the data necessary to start writing out data sets
        node.List.write_data_sets(global_data, node_list, config, output_dir)

        # Make graph files using the data_loader tools from Song Wang
        loader = CEBAFGraphLoader(data_path=output_dir, directed=True)
        loader.load_graph()
        loader.make_pickles()


        # Copy the config file we just used to the top level output directory so it can be
        # referenced as part of the data set.
        config_file = os.path.basename(args.config_file)
        shutil.copyfile(config_file, os.path.join(output_dir, 'config.yaml'))

        # Save the tree, nodes, and global data list to a file for later use?
        indent = 2
        if args.save_json:
            f = open(nodes_file, "w")
            print("[", file=f)
            i = 0
            for index, item in enumerate(progressBar(node_list, prefix='Write Json:', suffix='', length=60)):
                json.dump(item, f, cls=node.ListEncoder, indent=indent)
                if index < len(node_list) - 1:
                    print(",\n", file=f)
            print("]", file=f)
            f.close()

            f = open(globals_file, "w")
            json.dump(global_data, f, indent=indent)
            f.close()

            f = open(tree_file, "w")
            json.dump(tree.tree, f, indent=indent)
            f.close()

        exit(0)

    except json.JSONDecodeError as err:
        print(err)
        print("Oops!  Invalid JSON response. Check request parameters and try again.")
        exit(1)
    except RuntimeError as err:
        print("Exception: ", err)
        exit(1)
