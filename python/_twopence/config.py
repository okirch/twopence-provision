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

	def configure_children(self, config, block_name, factory):
		result = []
		for name in config.get_children(block_name):
			object = factory(name)
			object.configure(config.get_child(block_name, name))
			result.append(object)
		return result

class ConfigDict(dict):
	def __init__(self, type_name, item_class, verbose = False):
		self.type_name = type_name
		self.item_class = item_class
		self.verbose = verbose

	def create(self, name):
		item = self.get(name)
		if item is None:
			item = self.item_class(name)
			self[name] = item
		return item

	def configure(self, config):
		result = []
		for name in config.get_children(self.type_name):
			item = self.create(name)
			item.configure(config.get_child(self.type_name, name))
			result.append(item)

			if self.verbose:
				debug("Defined %s" % item)
		return result

class ExtraInfo:
	def __init__(self):
		self.data = {}

	# Config files can specify opaque bits of info that can be referenced
	# in template files. Example:
	#
	#	info "registration" {
	#		email "Olaf.Kirch@suse.com";
	#		regcode "INTERNAL-USE-ONLY-0000-0000";
	#	}
	#
	# We stow these in the info dict with keys registration_email
	# and registration_regcode. Template files can reference these.
	# Information from a global info {} group is provided using
	# the prefix "INFO_", while data from an info group nested within
	# a platform is provided with a prefix of "PLATFORM_INFO_".
	#
	# So if the above info group is global, a Vagrantfile template
	# would reference them as @INFO_REGISTRATION_EMAIL@ and
	# @INFO_REGISTRATION_REGCODE@, # respectively.
	def configure(self, config):
		for name in config.get_children("info"):
			child = config.get_child("info", name)

			for attr_name in child.get_attributes():
				values = child.get_values(attr_name)
				if not values:
					values = [""]

				info_name = "info_%s_%s" % (name, attr_name)
				self.info[attr_name] = values[0]
				self.info[attr_name + "_list"] = values

	def items(self):
		return self.data.items()

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
		self.repositories = ConfigDict("repository", Repository)
		self.features = []
		self.vendor = None
		self.os = None

		self.info = ExtraInfo()

	def configure(self, config):
		if not config:
			return

		self.update_value(config, 'image')
		self.update_value(config, 'keyfile')
		self.update_value(config, 'keyfile', 'ssh-keyfile')
		self.update_list(config, 'features')
		self.update_value(config, 'vendor')
		self.update_value(config, 'os')

		self.repositories.configure(config)

		# Extract info "blah" { ... } groups from the platform config.
		self.info.configure(config)

	def __str__(self):
		return "Platform(%s, image=%s)" % (self.name, self.image)

	def getRepository(self, name):
		return self.repositories.get(name)

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
		self.info = None

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
	def __init__(self, node, platform, global_info):
		super().__init__(node.name)

		self.platform = platform
		self.install += node.install
		self.start += node.start
		self.features += platform.features
		self.info = global_info

class SavedBackendConfig:
	def __init__(self, name, config = None):
		self.name = name
		self.configs = []
		if config:
			self.configs.append(config)

	def configure(self, config):
		self.configs.append(config)

class Config(Configurable):
	def __init__(self, workspace):
		self.workspace = workspace
		self.logspace = None
		self.testcase = None
		self.status = None

		self._backends = ConfigDict("backend", SavedBackendConfig)
		self._platforms = ConfigDict("platform", Platform, verbose = True)
		self._roles = ConfigDict("role", Role, verbose = True)
		self._nodes = ConfigDict("node", Node, verbose = True)
		self._repositories = []

		self.info = ExtraInfo()

		self.defaultRole = self._roles.create("default")

		self._valid = False

	def load(self, filename):
		if not os.path.exists(filename):
			return

		debug("Loading %s" % filename)
		config = curly.Config(filename)

		self.configure(config.tree())

	def configure(self, tree):
		self._backends.configure(tree)
		self._platforms.configure(tree)
		self._roles.configure(tree)
		self._nodes.configure(tree)

		self.update_value(tree, 'workspaceRoot', 'workspace-root')
		self.update_value(tree, 'workspace')
		self.update_value(tree, 'testcase')

		# Extract data from global info "blah" { ... } groups
		self.info.configure(tree)

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

	@property
	def platforms(self):
		return self._platforms.values()

	def getPlatform(self, name):
		return self._platforms.get(name)

	@property
	def roles(self):
		return self._roles.values()

	def getRole(self, name):
		return self._roles.get(name)

	@property
	def nodes(self):
		return self._nodes.values()

	def getNode(self, name):
		return self._nodes.get(name)

	def configureBackend(self, backend):
		saved = self._backends.get(backend.name)
		if saved and saved.configs:
			debug("Applying %s configs to backend %s" % (len(saved.configs), saved.name))
			for config in saved.configs:
				backend.configure(config)

	def finalizeNode(self, node):
		platform = self.platformForRole(node.role)

		if not platform.vendor or not platform.os:
			raise ConfigError("Node %s uses platform %s, which lacks a vendor and os definition" % (platform.name, node.name))

		result = FinalNodeConfig(node, platform, self.info)

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
