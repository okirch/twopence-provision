##################################################################
#
# Main class for the twopence provisioner: the topology class
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import susetest
import curly
import os
import time

from .config import ConfigError
from .instance import *
from .logging import *

class TopologyStatus:
	class NodeStatus:
		def __init__(self, config):
			self._config = config
			self.name = config.name
			self._ipv4_address = config.get_value("ipv4_address")
			self._ipv6_address = config.get_value("ipv6_address")
			self._features = config.get_values("features")
			self._resources = config.get_values("resources")
			self._vendor = config.get_value("vendor")
			self._os = config.get_value("os")
			self._keyfile = config.get_value("keyfile")

		@property
		def ipv4_address(self):
			return self._ipv4_address

		@ipv4_address.setter
		def ipv4_address(self, value):
			if self._ipv4_address != value:
				self._config.set_value("ipv4_address", value)
				self._ipv4_address = value

		@property
		def ipv6_address(self):
			return self._ipv6_address

		@ipv6_address.setter
		def ipv6_address(self, value):
			if self._ipv6_address != value:
				self._config.set_value("ipv6_address", value)
				self._ipv6_address = value

		@property
		def features(self):
			return self._features

		@features.setter
		def features(self, value):
			if self._features != value:
				self._config.set_value("features", value)
				self._features = value

		@property
		def resources(self):
			return self._resources

		@resources.setter
		def resources(self, value):
			if self._resources != value:
				self._config.set_value("resources", value)
				self._resources = value

		@property
		def vendor(self):
			return self._vendor

		@vendor.setter
		def vendor(self, value):
			if self._vendor != value:
				self._config.set_value("vendor", value)
				self._vendor = value

		@property
		def os(self):
			return self._os

		@os.setter
		def os(self, value):
			if self._os != value:
				self._config.set_value("os", value)
				self._os = value

		@property
		def keyfile(self):
			return self._keyfile

		def clearNetwork(self):
			self.ipv4_address = None
			self.ipv6_address = None

		def set_value(self, name, value):
			self._config.set_value(name, value)

	def __init__(self, pathname):
		self.path = pathname

		if self.path.startswith("~"):
			raise ConfigError("Invalid status path \"%s\"" % self.path)

		# If the status file exists, read it. Otherwise
		# start with an empty status object
		if self.path and os.path.exists(self.path):
			self._load()
		else:
			self.data = curly.Config()
			self.tree = self.data.tree()

			self._backend = None
			self._testcase = None
			self._logspace = None
			self._parameters = {}
			self._nodes = {}

		self.tree = self.data.tree()

	def _load(self):
		self.data = curly.Config(self.path)
		debug("Loaded status from %s" % self.path)

		self.tree = self.data.tree()

		self._backend = self.tree.get_value("backend")
		self._testcase = self.tree.get_value("testcase")
		self._workspace = self.tree.get_value("workspace")
		self._logspace = self.tree.get_value("logspace")

		self._nodes = {}
		for name in self.tree.get_children("node"):
			self._nodes[name] = self.NodeStatus(self.tree.get_child("node", name))

		self._parameters = {}
		child = self.tree.get_child("parameters")
		if child:
			for name in child.get_attributes():
				self._parameters[name] = child.get_value(name)

	@property
	def backend(self):
		return self._backend

	@backend.setter
	def backend(self, value):
		if self._backend != value:
			self.tree.set_value("backend", value)
			self._backend = value

	@property
	def testcase(self):
		return self._testcase

	@testcase.setter
	def testcase(self, value):
		if self._testcase != value:
			self.tree.set_value("testcase", value)
			self._testcase = value

	@property
	def workspace(self):
		return self._workspace

	@workspace.setter
	def workspace(self, value):
		if self._workspace != value:
			self.tree.set_value("workspace", value)
			self._workspace = value

	@property
	def logspace(self):
		return self._logspace

	@logspace.setter
	def logspace(self, value):
		if self._logspace != value:
			self.tree.set_value("logspace", value)
			self._logspace = value

	@property
	def parameters(self):
		return self._parameters

	@parameters.setter
	def parameters(self, value_dict):
		if self._parameters != value_dict:
			# If we have a parameters section already, drop it...
			child = self.tree.get_child("parameters")
			if child is not None:
				self.tree.drop_child(child)

			# ... then (re)create it and add all the key/value pairs.
			child = self.tree.add_child("parameters")
			for name, value in value_dict.items():
				child.set_value(name, value)

			self._parameters = value_dict

	@property
	def nodes(self):
		return self._nodes.values()

	def getNodeState(self, name, create = False):
		node = self._nodes.get(name)
		if node is None and create:
			node = self.NodeStatus(self.tree.add_child("node", name))
			self._nodes[name] = node
		return node

	def createNodeState(self, name):
		return self.getNodeState(name, create = True)

	def dropNode(self, node):
		debug("dropping status for node %s" % node.name)
		if self.tree.drop_child(node._config) == 0:
			print("drop_child(%s) failed" % node)

		try:
			del self._nodes[node.name]
		except: pass

	def save(self):
		if not self.path:
			raise ValueError("%s: cannot save data, pathname not set" % self.__class__.__name__)

		parent_dir = os.path.dirname(self.path)
		if parent_dir and not os.path.isdir(parent_dir):
			debug("Creating directory %s" % parent_dir)
			os.makedirs(parent_dir)

		debug("Saving status to %s" % self.path)
		self.data.save(self.path)

		if False:
			print("-- contents of %s --" % self.path)
			os.system("cat %s" % self.path)
			print("-- END of contents --")

	def remove(self):
		if self.path and os.path.exists(self.path):
			os.remove(self.path)


class TestTopology:
	def __init__(self, backend, config = None, workspace = None):
		self.backend = backend
		self.workspace = workspace

		self.testcase = None
		self.logspace = None
		self.platform = None
		self.parameters = {}
		self.persistentState = None
		self.persistentStatePath = None

		self.instanceConfigs = []
		self.instances = []

		if config:
			self.configure(config)

		if not os.path.isdir(self.workspace):
			os.makedirs(self.workspace)

		# Attach persistent state (and load it if it exists)
		path = self.persistentStatePath
		if path is None:
			path = os.path.join(self.workspace, "status.conf")
		self.persistentState = TopologyStatus(path)

		# Write back persistent state if it does not exist.
		if not os.path.isfile(path):
			self.saveStatus()

		backend.testcase = self.testcase
		assert(self.testcase)

	def configure(self, config):
		config.validate()

		self.testcase = config.testcase
		self.workspace = config.workspace
		self.logspace = config.logspace
		self.persistentStatePath = config.status

		for node in config.nodes:
			self.createInstanceConfig(node, config)

		if config.parameters:
			self.parameters.update(config.parameters)

		config.configureBackend(self.backend)

	def saveStatus(self):
		if self.persistentState:
			self.persistentState.backend = self.backend.name
			self.persistentState.testcase = self.testcase
			self.persistentState.logspace = self.logspace
			self.persistentState.parameters = self.parameters
			self.persistentState.save()

	def cleanupStatus(self):
		if self.persistentState:
			self.persistentState.remove()

	def hasRunningInstances(self):
		return any(i.running for i in self.instances)

	def createInstance(self, instanceConfig):
		instanceWorkspace = os.path.join(self.workspace, instanceConfig.name)
		instanceState = self.persistentState.createNodeState(instanceConfig.name)
		return self.backend.createInstance(instanceConfig, instanceWorkspace, instanceState)

	def createAllInstances(self, includeStaleInstances = False):
		found = []

		for instanceConfig in self.instanceConfigs:
			instance = self.createInstance(instanceConfig)
			found.append(instance)

		if includeStaleInstances:
			# Loop over all nodes defined in status.conf - the user may have messed with the test config
			# file and added/removed nodes.
			for savedInstanceState in self.persistentState.nodes:
				if any(instance.name == savedInstanceState.name for instance in found):
					continue

				dummyConfig = Config.createEmptyNode(savedInstanceState.name)
				instance = self.createInstance(dummyConfig)
				found.append(instance)

		return found

	def detect(self, detectNetwork = False):
		found = self.createAllInstances(includeStaleInstances = True)
		self.instances = self.backend.detect(self, found)
		return self.instances

	def requires(self):
		for instanceConfig in self.instanceConfigs:
			for name in instanceConfig.requires:
				yield name, instanceConfig.name

	def prepare(self):
		assert(not self.instances)

		self.saveStatus()

		success = True
		instances = self.createAllInstances()

		for instance in instances:
			if not self.backend.downloadImage(instance):
				raise ValueError(f"Failed to download image for instance {instance.name}")

		for instance in instances:
			# Create the workspace directory of this instance.
			# This throws an exception in case of errors
			instance.createWorkspace()

			self.backend.prepareInstance(instance)
			if instance.exists:
				error("Ouch, instance %s seems to exist" % instance.name)
				success = False

			self.instances.append(instance)

		self.saveStatus()
		return success

	def start(self, okayIfRunning = False):
		if any(i.exists for i in self.instances):
			print("Refusing to start; please clean up any existing instances first");
			return False

		success = True
		for instance in self.instances:
			if instance.running:
				if not okayIfRunning:
					raise ValueRrror("Instance %s already running" % instance.name)
				continue

			if verbose_enabled():
				verbose("  Image %s, SSH keyfile %s" % (instance.config.image, instance.config.keyfile))
				if instance.config.install:
					verbose("  Installing package(s):")
					for name in instance.config.install:
						verbose("        %s" % name)
				if instance.config.start:
					verbose("  Starting service(s):")
					for name in instance.config.start:
						verbose("        %s" % name)

			if not instance.persistent:
				print("Oops, no persistent state for %s?!" % instance.name)
				fail

			try:
				success = self.backend.startInstance(instance)
			except Exception as e:
				import traceback

				print("Caught exception while trying to start instance: %s" % e)
				traceback.print_exc()
				success = False

			if not success:
				print("Failed to start instance %s" % instance.name)
				break

			instance.exists = True
			instance.running = True

			self.backend.updateInstanceTarget(instance)

			self.saveStatus()

		return success

	def stop(self, **kwargs):
		for instance in self.instances:
			self.backend.stopInstance(instance, **kwargs)
			self.backend.updateInstanceTarget(instance)

			self.saveStatus()

	def package(self, nodeName, packageName):
		instance = self.getInstance(nodeName)
		if instance is None:
			raise ValueError("Cannot package %s: instance not found" % nodeName)

		return self.backend.packageInstance(instance, packageName)

	def destroy(self):
		for instance in self.instances:
			self.backend.destroyInstance(instance)

			if instance.persistent:
				self.persistentState.dropNode(instance.persistent)
				instance.persistent = None

			self.saveStatus()
		self.instances = []

	def cleanup(self):
		self.cleanupStatus()

		# Do not try to remove the workspace; it contains the BOM file
		# and possibly copies of some config files

	def createInstanceConfig(self, node, config):
		nodeConfig = config.finalizeNode(node, self.backend)
		self.instanceConfigs.append(nodeConfig)
		return nodeConfig

	def getInstance(self, name):
		for instance in self.instances:
			if instance.name == name:
				return instance
		return None
