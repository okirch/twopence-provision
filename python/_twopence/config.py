##################################################################
#
# config handling for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import susetest
import curly
import os
import time

from .instance import *
from .logging import *

class ConfigError(Exception):
	pass

##################################################################
# This is a helper class that simplifies how we populate a
# python object from a curly config file.
##################################################################
class Configurable:
	def update_value(self, config, attr_name, config_key = None):
		if config_key is None:
			config_key = attr_name
		value = config.get_value(config_key)
		if value is not None:
			setattr(self, attr_name, value)

	def update_list(self, config, attr_name):
		# get_values may return None or []
		value = config.get_values(attr_name)
		if value:
			current = getattr(self, attr_name)
			assert(type(current) == list)
			setattr(self, attr_name, current + value)

class Repository(Configurable):
	def __init__(self, name):
		self.name = name
		self.url = None
		self.keyfile = None

	def configure(self, config):
		if not config:
			return

		self.update_value(config, 'url')
		self.update_value(config, 'keyfile')

	def __str__(self):
		return "Repository(%s, url=%s)" % (self.name, self.url)

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

	def __str__(self):
		return "Platform(%s, image=%s)" % (self.name, self.image)

	def getRepository(self, name):
		return self.repositories.get(name)

	def createRepository(self, name):
		repo = self.repositories.get(name)
		if repo is None:
			repo = Repository(name)
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

	def __str__(self):
		return "Role(%s, platform=%s)" % (self.name, self.platform)

class Node(Configurable):
	def __init__(self, name):
		self.name = name
		self.role = name
		self.install = []
		self.start = []

		# fields set in finalize()
		self._platform = None

	def configure(self, config):
		if not config:
			return

		self.update_value(config, 'role')
		self.update_list(config, 'install')
		self.update_list(config, 'start')

	def __str__(self):
		return "Node(%s, role=%s)" % (self.name, self.role)

class EmptyNodeConfig:
	def __init__(self, name):
		self.name = name
		self.role = None
		self.platform = None
		self.repositories = []
		self.install = []
		self.start = []
		self.features = []

	@property
	def image(self):
		if not self.platform:
			return None
		return self.platform.image

	@property
	def keyfile(self):
		if not self.platform:
			return None
		return self.platform.keyfile

	@property
	def vendor(self):
		if not self.platform:
			return None
		return self.platform.vendor

	@property
	def os(self):
		if not self.platform:
			return None
		return self.platform.os

	def fromRole(self, role):
		if not role:
			return

		for name in role.repositories:
			repo = self.platform.getRepository(name)
			if repo is None:
				raise ConfigError("instance %s wants to use repository %s, but platform %s does not define it" % (
							self.name, name, self.platform.name))

			if repo not in self.repositories:
				self.repositories.append(repo)

		for name in role.install:
			if name not in self.install:
				self.install.append(name)

		for name in role.start:
			if name not in self.start:
				self.start.append(name)

		self.features += role.features

	def persistInfo(self, nodePersist):
		nodePersist.features = self.features
		if self.platform:
			nodePersist.vendor = self.platform.vendor
			nodePersist.os = self.platform.os

class FinalNodeConfig(EmptyNodeConfig):
	def __init__(self, node, platform):
		super().__init__(node.name)

		self.platform = platform
		self.install += node.install
		self.start += node.start
		self.features += platform.features

class SavedBackendConfig:
	def __init__(self, name, config):
		self.name = name
		self.config = config

class Config(Configurable):
	def __init__(self, workspace):
		self.workspace = workspace
		self.logspace = None
		self.testcase = None
		self.status = None

		self.backends = []
		self._platforms = {}
		self._roles = {}
		self._nodes = {}
		self._repositories = []

		self.defaultRole = self.createRole("default")

		self._valid = False

	def load(self, filename):
		if not os.path.exists(filename):
			return

		debug("Loading %s" % filename)
		config = curly.Config(filename)

		self.configure(config.tree())

	def configure(self, tree):
		self.configureObjects(tree, "platform", self.createPlatform)
		self.configureObjects(tree, "role", self.createRole)
		self.configureObjects(tree, "node", self.createNode)

		self.update_value(tree, 'workspaceRoot', 'workspace-root')
		self.update_value(tree, 'workspace')
		self.update_value(tree, 'testcase')

		for name in tree.get_children("backend"):
			child = tree.get_child("backend", name)
			self.backends.append(SavedBackendConfig(name, child))

	def validate(self):
		if self._valid:
			return

		if not self.testcase:
			raise ConfigError("no testcase name configured")

		if not self.workspace:
			raise ConfigError("no workspace configured")

		if not self.nodes:
			raise ConfigError("no nodes configured")

		self._valid = True

	def configureObjects(self, config, config_key, factory):
		result = []
		for name in config.get_children(config_key,):
			child = config.get_child(config_key, name)

			object = factory(name)
			object.configure(child)
			result.append(object)

			debug("Defined %s" % object)

		return result

	@property
	def platforms(self):
		return self._platforms.values()

	def getPlatform(self, name):
		return self._platforms.get(name)

	def createPlatform(self, name):
		platform = self._platforms.get(name)
		if platform is None:
			platform = Platform(name)
			self._platforms[name] = platform
		return platform

	@property
	def roles(self):
		return self._roles.values()

	def getRole(self, name):
		return self._roles.get(name)

	def createRole(self, name):
		role = self._roles.get(name)
		if role is None:
			role = Role(name)
			self._roles[name] = role
		return role

	@property
	def nodes(self):
		return self._nodes.values()

	def getNode(self, name):
		return self._nodes.get(name)

	def createNode(self, name):
		node = self._nodes.get(name)
		if node is None:
			node = Node(name)
			self._nodes[name] = node
		return node

	def configureBackend(self, backend):
		for saved in self.backends:
			if saved.name == backend.name:
				debug("Applying %s backend config" % saved.name)
				backend.configure(saved.config)

	def finalizeNode(self, node):
		platform = self.platformForRole(node.role)

		result = FinalNodeConfig(node, platform)

		role = self.getRole("default")
		if role:
			result.fromRole(role)

		role = self.getRole(node.role)
		if role:
			result.fromRole(role)

		return result

	@staticmethod
	def createEmptyNode(name, workspace = None):
		return EmptyNodeConfig(name)

	def platformForRole(self, roleName):
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
