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

from _twopence.provision import ConfigError
from .util import Configurable
from .instance import *
from .logging import *

class TopologyStatus:
	class NodeStatus:
		def __init__(self, config):
			self._config = config
			self.name = config.name()
			self._ipv4_address = config.get_value("ipv4_address")
			self._ipv6_address = config.get_value("ipv6_address")
			self._features = config.get_values("features")
			self._vendor = config.get_value("vendor")
			self._os = config.get_value("os")

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
	class Repository(Configurable):
		def __init__(self, name):
			self.name = name
			self.url = None
			self.keyfile = None

		def configure(self, config):
			if not config:
				return

			self.update_value(config, 'url')

	class Platform(Configurable):
		def __init__(self, name):
			self.name = name
			self.image = None
			self.keyfile = None
			self.repositories = {}
			self.features = []
			self.vendor = []
			self.os = []

		def configure(self, config):
			if not config:
				return

			self.update_value(config, 'image')
			self.update_value(config, 'keyfile')
			self.update_value(config, 'keyfile', 'ssh-keyfile')
			self.update_list(config, 'features')
			self.update_value(config, 'vendor')
			self.update_value(config, 'os')

			for name in config.get_children("repository"):
				child = config.get_child("repository", name)

				repo = self.createRepository(name)
				repo.configure(child)

				# print("Platform %s provides repo %s at %s" % (self.name, repo.name, repo.url))

		def getRepository(self, name):
			return self.repositories.get(name)

		def createRepository(self, name):
			repo = self.repositories.get(name)
			if repo is None:
				repo = TestTopology.Repository(name)
				self.repositories[name] = repo
			return repo

	class Role(Configurable):
		def __init__(self, name):
			self.name = name
			self.platform = None

			self.repositories = []
			self.install = []
			self.start = []
			self.features = []

		def configure(self, config):
			if not config:
				return

			self.update_value(config, 'platform')
			self.update_list(config, 'repositories')
			self.update_list(config, 'install')
			self.update_list(config, 'start')
			self.update_list(config, 'features')

	class Node(Configurable):
		def __init__(self, name):
			self.name = name
			self.role = name
			self.install = []
			self.start = []

		def configure(self, config):
			if not config:
				return

			self.update_value(config, 'role')
			self.update_list(config, 'install')
			self.update_list(config, 'start')

	def __init__(self, backend, workspace = None):
		self.backend = backend
		self.workspace = workspace

		self.platforms = {}
		self.roles = {}
		self.nodes = {}
		self.repositories = []
		self.testcase = None
		self.workspaceRoot = None
		self.logspace = None
		self.platform = None
		self.persistentState = None
		self.persistentStatePath = None
		self.defaultRole = self.createRole("default")

		self.instanceConfigs = []
		self.instances = []

		self._valid = False

	def loadConfig(self, filename):
		if not os.path.exists(filename):
			return

		config = curly.Config(filename)

		tree = config.tree()

		self.platformsFromConfig(tree)
		self.rolesFromConfig(tree)
		self.nodesFromConfig(tree)

		workspaceRoot = tree.get_value('workspace-root')
		if workspaceRoot:
			self.workspaceRoot = workspaceRoot

		workspace = tree.get_value('workspace')
		if workspace:
			self.workspace = workspace

		testcase = tree.get_value('testcase')
		if testcase:
			self.testcase = testcase

		child = tree.get_child("backend", self.backend.name)
		if child:
			self.backend.configure(child)

	def setStatusPath(self, pathname):
		self.persistentStatePath = pathname

	def loadStatus(self):
		if not self.persistentState:
			path = self.persistentStatePath
			if path is None:
				path = os.path.join(self.workspace, "status.conf")
			self.persistentState = TopologyStatus(path)
		return self.persistentState

	def saveStatus(self):
		if self.persistentState:
			self.persistentState.backend = self.backend.name
			self.persistentState.testcase = self.testcase
			self.persistentState.logspace = self.logspace
			self.persistentState.save()

	def cleanupStatus(self):
		if self.persistentState:
			self.persistentState.remove()

	def validateConfig(self):
		if self._valid:
			return

		if not self.testcase:
			raise ConfigError("no testcase name configured")

		if not self.workspace and self.workspaceRoot:
			self.workspace = os.path.join(self.workspaceRoot, self.testcase, time.strftime("%Y%m%dT%H%M%S"))

		if not self.workspace:
			raise ConfigError("no workspace configured")

		if not os.path.isdir(self.workspace):
			os.makedirs(self.workspace)

		status = self.loadStatus()

		status.backend = self.backend.name
		status.testcase = self.testcase
		status.save()

		for node in self.nodes.values():
			self.createInstanceConfig(node)

		self._valid = True

	def loadNodeFile(self, nodepath):
		instances = []

		f = susetest.NodesFile(nodepath)
		for oldNode in f.nodes:
			node = self.createNode(oldNode.name)
			if oldNode.role:
				node.role = oldNode.role

			node.install = oldNode.installPackages

		if not self.testcase:
			w = nodepath.split('/')
			while w:
				name = w.pop()
				if not name or name == 'nodes' or name == 'twopence':
					continue

				if name.startswith("twopence-"):
					name = name[9:]

				self.testcase = name

		return

	def setupRepositories(self, repositories):
		if not repositories:
			return # [] or None

		for url in repositories:
			self.repositories.append(url)

	def hasRunningInstances(self):
		return any(i.running for i in self.instances)

	def detect(self, detectNetwork = False):
		self.instances = []

		status = self.loadStatus()

		if self.testcase is None:
			self.testcase = status.testcase
			if self.testcase is None:
				return

			debug("Detected testcase %s" % self.testcase)

		self.validateConfig()

		self.instances = self.backend.detect(self.workspace, self.persistentState, self.instanceConfigs)

		return self.instances

	def prepare(self):
		assert(not self.instances)

		self.validateConfig()
		self.saveStatus()

		success = True
		for instanceConfig in self.instanceConfigs:
			instance = self.backend.prepareInstance(self.workspace, instanceConfig,
						self.persistentState.createNodeState(instanceConfig.name))
			if instance.exists:
				print("Ouch, instance %s seems to exist" % instance.name)
				success = False

			self.instances.append(instance)

		self.saveStatus()
		return success

	def start(self, okayIfRunning = False):
		self.validateConfig()

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
				print("Caught exception while trying to start instance: %s" % e)
				success = False

			if not success:
				print("Failed to start instance %s" % instance.name)
				break

			instance.exists = True

			self.backend.updateInstanceTarget(instance)

			self.saveStatus()

		return success

	def stop(self, **kwargs):
		self.validateConfig()

		for instance in self.instances:
			self.backend.stopInstance(instance, **kwargs)
			self.backend.updateInstanceTarget(instance)

			self.saveStatus()

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

	def createInstanceConfig(self, node):
		result = InstanceConfig(node.name)

		role = self.getRole(node.role)

		result.platform = self._platformForRole(node.role)
		result.image = result.platform.image
		result.keyfile = result.platform.keyfile

		result.installPackages(node.install)
		result.startServices(node.start)
		result.enableFeatures(result.platform.features)

		if role:
			result.enableRepositories(role.repositories)
			result.installPackages(role.install)
			result.enableFeatures(role.features)

		result.enableRepositories(self.defaultRole.repositories)
		result.installPackages(self.defaultRole.install)
		result.enableFeatures(self.defaultRole.features)

		self.instanceConfigs.append(result)
		return result

	def _platformForRole(self, roleName):
		role = self.getRole(roleName)
		if role and role.platform:
			platform = self.getPlatform(role.platform)
			if platform:
				return platform

			raise ValueError("Cannot find platform \"%s\" for role \"%s\"" % (role.platform, node.role))

		if self.defaultRole.platform:
			platform = self.getPlatform(self.defaultRole.platform)
			if platform:
				return platform

			raise ValueError("Cannot find platform \"%s\" for default role" % (self.defaultRole.platform))

		raise ValueError("No platform defined for role \"%s\"" % roleName)

	def platformsFromConfig(self, tree):
		for name in tree.get_children("platform"):
			platform = self.createPlatform(name)

			child = tree.get_child("platform", name)
			platform.configure(child)

			debug("Defined platform %s image=%s key=%s" % (platform.name,
				platform.image, platform.keyfile))

	def rolesFromConfig(self, tree):
		for name in tree.get_children("role"):
			child = tree.get_child("role", name)

			role = self.createRole(name)
			role.configure(child)

			debug("Defined role %s platform=%s repos=%s" % (role.name,
				role.platform, role.repositories))

	def nodesFromConfig(self, tree):
		for name in tree.get_children("node"):
			child = tree.get_child("node", name)

			node = self.createNode(name)
			node.configure(child)

	def getPlatform(self, name):
		return self.platforms.get(name)

	def createPlatform(self, name):
		platform = self.platforms.get(name)
		if platform is None:
			platform = self.Platform(name)
			self.platforms[name] = platform
		return platform

	def getRole(self, name):
		return self.roles.get(name)

	def createRole(self, name):
		role = self.roles.get(name)
		if role is None:
			role = self.Role(name)
			self.roles[name] = role
		return role

	def getNode(self, name):
		return self.nodes.get(name)

	def createNode(self, name):
		node = self.nodes.get(name)
		if node is None:
			node = self.Node(name)
			self.nodes[name] = node
		return node
