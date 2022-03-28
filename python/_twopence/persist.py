##################################################################
#
# Persist info on the topology between states of provisioning,
# build, packaging, etc.
#
# Copyright (C) 2021, 2022 Olaf Kirch <okir@suse.de>
#
##################################################################

import susetest
import curly
import os
import time

from .logging import *
from .config import *
from .runtime import *

class NodeResource(NamedConfigurable):
	info_attrs = ['name']

class NodeVolumeResource(NodeResource):
	schema = [
		StringAttributeSchema('mountpoint'),
		StringAttributeSchema('fstype'),
		StringAttributeSchema('host_path', 'host-path'),
	]

class NodePortResource(NodeResource):
	schema = [
		StringAttributeSchema('protocol'),
		IntegerAttributeSchema('internal_port', 'internal-port'),
		IntegerAttributeSchema('external_port', 'external-port'),
	]

class NodeApplicationResources(Configurable):
	schema = [
		DictNodeSchema('_volumes', 'volume', itemClass = NodeVolumeResource),
		DictNodeSchema('_ports', 'port', itemClass = NodePortResource),
		DictNodeSchema('_files', 'file', itemClass = ConfigOpaque),
		DictNodeSchema('_directories', 'directory', itemClass = ConfigOpaque),
	]

class NodeContainerStatus(Configurable):
	schema = [
		StringAttributeSchema('backend'),
		StringAttributeSchema('name'),
		StringAttributeSchema('id'),
		IntegerAttributeSchema('pid'),
	]

class NodeStatus(NamedConfigurable):
	info_attrs = ['name', 'os', 'ipv4_address', 'ipv6_address']

	schema = [
		StringAttributeSchema('ipv4_address'),
		StringAttributeSchema('ipv6_address'),
		ListAttributeSchema('features'),
		ListAttributeSchema('resources'),
		StringAttributeSchema('vendor'),
		StringAttributeSchema('os'),
		StringAttributeSchema('application'),
		StringAttributeSchema('application_class', 'application-class'),
		StringAttributeSchema('keyfile'),
		StringAttributeSchema('image'),
		StringAttributeSchema('start_time', 'start-time'),
		StringAttributeSchema('target'),
		DictNodeSchema('_built', 'built', itemClass = Platform),
		DictNodeSchema('_loop_devices', 'loop-device', itemClass = LoopDevice),
		SingleNodeSchema('application_resources', 'application-resources', itemClass = NodeApplicationResources),
		SingleNodeSchema('_container', 'container', itemClass = NodeContainerStatus),
	]

	def __init__(self, name, config = None):
		assert(type(name) == str)
		super().__init__(name)

		self.application_resources = NodeApplicationResources()
		self._config = config

	def clearNetwork(self):
		self.ipv4_address = None
		self.ipv6_address = None

	@property
	def loop_devices(self):
		return self._loop_devices

	def createLoopDevice(self, name):
		return self._loop_devices.create(name)

	def addLoopDevice(self, dev):
		assert(isinstance(dev, LoopDevice))
		self._loop_devices[dev.name] = dev

	@property
	def container(self):
		if self._container is None:
			self._container = NodeContainerStatus()
		return self._container

class TopologyStatus(Configurable):
	info_attrs = ['testcase']

	schema = [
		StringAttributeSchema('backendName', 'backend'),
		StringAttributeSchema('testcase'),
		StringAttributeSchema('logspace'),
		ParameterNodeSchema('_parameters', 'parameters'),
		DictNodeSchema('_suts', 'node', itemClass = NodeStatus),
	]

	def __init__(self, pathname):
		super().__init__()

		# If the status file exists, read it. Otherwise
		# start with an empty status object
		self.path = pathname
		if self.path.startswith("~"):
			raise ConfigError("Invalid status path \"%s\"" % self.path)
		if os.path.exists(self.path):
			self.configureFromPath(self.path)

	@property
	def nodes(self):
		return self._suts.values()

	def getNodeState(self, name, create = False):
		node = self._suts.get(name)
		if node is None and create:
			node = self._suts.create(name)
		return node

	def createNodeState(self, name):
		return self.getNodeState(name, create = True)

	def dropNode(self, node):
		debug(f"dropping status for node {node.name}")
		try:
			del self._suts[node.name]
		except: pass

	def save(self):
		if not self.path:
			raise ValueError("%s: cannot save data, pathname not set" % self.__class__.__name__)

		parent_dir = os.path.dirname(self.path)
		if parent_dir and not os.path.isdir(parent_dir):
			debug("Creating directory %s" % parent_dir)
			os.makedirs(parent_dir)

		debug("Saving status to %s" % self.path)
		self.publishToPath(self.path)

		if False:
			print("-- contents of %s --" % self.path)
			os.system("cat %s" % self.path)
			print("-- END of contents --")

	def remove(self):
		if self.path and os.path.exists(self.path):
			os.remove(self.path)

class PersistentTestTopology(ConfigFacade):
	facadedClass = TopologyStatus

	def __init__(self, path):
		super().__init__(path)

class PeristentTestInstance(ConfigFacade):
	facadedClass = NodeStatus

	def __init__(self, backingObject):
		super().__init__(backingObject = backingObject)
		self._platform = None

	@property
	def persistent(self):
		return self._backingObject

	def fromNodeConfig(self, instanceConfig):
		self.name = instanceConfig.name
		self.features = instanceConfig.features
		self.resources = instanceConfig.resources

		platform = instanceConfig.platform
		if platform:
			self.vendor = platform.vendor
			self.os = platform.os
			if platform.isApplication:
				self.application = platform.id
				self.application_class = platform.application_class

		# This is a bit complicated, but the reason is this
		# For every stage of twopence-provision, we have to reload
		# the configuration (obviously, because we're a new process).
		# This means that the FinalNodeConfig will also reconstruct the
		# buildResult. If we just blindly set self.buildResult here,
		# we will overwrite what we just loaded from status.conf
		if self.buildResult is None:
			self.buildResult = instanceConfig.buildResult
		else:
			buildResult = self.buildResult

		buildResult = self.buildResult

	@property
	def buildResult(self):
		if self._platform:
			return self._built.get(self._platform)

		loaded = list(self._built.values())
		if loaded and len(loaded) == 1:
			platform = loaded[0]
			self._platform = platform.name
			return platform

	@buildResult.setter
	def buildResult(self, platform):
		if platform is None:
			if self._platform:
				# not sure if ConfigDict supports this right now
				del self._built[self._platform]

		self._built[platform.name] = platform
		self._platform = platform.name

Schema.initializeAll(globals())
