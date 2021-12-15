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
import shutil

from .instance import *
from .logging import *
from .paths import *

class ConfigError(Exception):
	pass

##################################################################
# This is a helper class that simplifies how we populate a
# python object from a curly config file.
##################################################################
class Configurable:
	info_attrs = []

	def update_value(self, config, attr_name, config_key = None, typeconv = None):
		if config_key is None:
			config_key = attr_name
		value = config.get_value(config_key)
		if value is not None:
			if typeconv:
				value = typeconv(value)
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

	def __str__(self):
		info = []
		for attr_name in self.info_attrs:
			value = getattr(self, attr_name, None)
			if not value:
				continue
			if attr_name == 'name':
				info.append(value)
			else:
				info.append("%s=%s" % (attr_name, value))
		return "%s(%s)" % (self.__class__.__name__, ", ".join(info))

class ConfigDict(dict):
	def __init__(self, type_name, item_class, verbose = False):
		self.type_name = type_name
		self.item_class = item_class
		self.verbose = verbose

	def __str__(self):
		return "[%s]" % " ".join([str(_) for _ in self.values()])

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

	def publish(self, config):
		for item in self.values():
			child = config.add_child(self.type_name, item.name)
			item.publish(child)

class ExtraInfo(dict):
	def __init__(self, name = None):
		self.name = name

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

				info_name = "%s_%s" % (name, attr_name)
				self[info_name] = values[0]
				self[info_name + "_list"] = values

class SavedBackendConfig:
	def __init__(self, name, config = None):
		self.name = name
		self.configs = []
		if config:
			self.configs.append(config)

	def configure(self, config):
		self.configs.append(config)

class BackendDict(ConfigDict):
	def __init__(self):
		super().__init__("backend", SavedBackendConfig)

	def savedConfigs(self, backendName):
		saved = self.get(backendName)
		if saved and saved.configs:
			return saved.configs
		return []

class Repository(Configurable):
	info_attrs = ['name', 'url']

	def __init__(self, name):
		self.name = name
		self.url = None
		self.keyfile = None

	def configure(self, config):
		if not config:
			return

		self.update_value(config, 'url')
		self.update_value(config, 'keyfile')

	def publish(self, config):
		if self.url:
			config.set_value("url", self.url)
		if self.keyfile:
			config.set_value("keyfile", self.keyfile)

class Image:
	def __init__(self, name, backends):
		self.name = name
		self.backends = backends

class Imageset(Configurable):
	info_attrs = ['name']

	class Architecture(Configurable):
		def __init__(self, name):
			self.name = name
			self.backends = BackendDict()

		def configure(self, config):
			self.backends.configure(config)

		def __str__(self):
			return "Imageset.Arch(%s)" % self.name

		def getBackend(self, name):
			return self.backends.get(name)

	def __init__(self, name):
		self.name = name
		self.architectures = ConfigDict("architecture", self.Architecture, verbose = True)

	def configure(self, config):
		self.architectures.configure(config)

	def getArchitecture(self, name):
		return self.architectures.get(name)

class Platform(Configurable):
	info_attrs = ['name', 'image', 'vendor', 'os', 'imagesets', 'requires', 'features']

	def __init__(self, name):
		self.name = name
		self.arch = None
		self.image = None
		self.keyfile = None
		self.repositories = ConfigDict("repository", Repository)
		self.imagesets = ConfigDict("imageset", Imageset)
		self.backends = BackendDict()
		self.requires = []
		self.features = []
		self.vendor = None
		self.os = None

		self.info = ExtraInfo()

		# used during build, exclusively
		self._raw_key = None

	def configure(self, config):
		if not config:
			return

		self.update_value(config, 'image')
		self.update_value(config, 'keyfile')
		self.update_value(config, 'keyfile', 'ssh-keyfile')
		self.update_list(config, 'requires')
		self.update_list(config, 'features')
		self.update_value(config, 'vendor')
		self.update_value(config, 'os')

		self.repositories.configure(config)
		self.imagesets.configure(config)
		self.backends.configure(config)

		# Extract info "blah" { ... } groups from the platform config.
		self.info.configure(config)

	def getRepository(self, name):
		return self.repositories.get(name)

	##########################################################
	# The remaining methods and properties are for newly
	# built silver images only
	##########################################################
	def addBackend(self, name, **kwargs):
		saved = self.backends.create(name)
		if saved.configs:
			config = saved.configs[0]
		else:
			config = curly.Config().tree()
			saved.configs.append(config)

		for key, value in kwargs.items():
			config.set_value(key, value)

	def finalize(self):
		if not self.keyfile and self._raw_key:
			self.saveKey(self._raw_key)

		if not self.keyfile:
			verbose("WARNING: backend did not capture an ssh key for %s" % self.name)

	def save(self):
		new_config = curly.Config()

		config = new_config.tree()
		child = config.add_child("platform", self.name)
		self.publish(child)

		path = os.path.join(self.platformdir, "%s.conf" % self.name)
		new_config.save(path)
		verbose("Saved platform config to %s" % path)

	def publish(self, config):
		config.set_value("vendor", self.vendor)
		config.set_value("os", self.os)
		if self.features:
			config.set_value("features", self.features)
		if self.keyfile:
			config.set_value("ssh-keyfile", self.keyfile)

		self.repositories.publish(config)

		for saved in self.backends.values():
			grand_child = config.add_child("backend", saved.name)
			for config in saved.configs:
				for attr_name in config.get_attributes():
					values = config.get_values(attr_name)
					if not values:
						values = [""]
					grand_child.set_value(attr_name, values)

	def getOutputDir(self, name):
		path = os.path.expanduser(twopence_user_data_dir)
		path = os.path.join(path, name)
		if not os.path.isdir(path):
			os.makedirs(path)
		return path

	def getImagePath(self, backend, imgfile):
		destdir = self.getOutputDir(backend)
		return os.path.join(destdir, imgfile)

	@property
	def datadir(self):
		path = os.path.expanduser(twopence_user_data_dir)
		path = os.path.join(path, self.name)
		if not os.path.isdir(path):
			os.makedirs(path)
		return path

	@property
	def platformdir(self):
		path = os.path.expanduser(twopence_user_config_dir)
		path = os.path.join(path, "platform.d")
		if not os.path.isdir(path):
			os.makedirs(path)
		return path

	def setRawKey(self, keyData):
		self._raw_key = keyData

	def saveKey(self, keyData):
		keyfile = "%s.key" % self.name
		keypath = os.path.join(self.datadir, keyfile)
		with open(keypath, "wb") as f:
			f.write(keyData)

		self.keyfile = keypath
		verbose("Saved captured SSH key to %s" % keypath)

	def makeImageVersion(self):
		return time.strftime("%Y%m%d.%H%M%S")

	def saveImage(self, backend, src):
		imgfile = os.path.basename(src)

		destdir = self.getOutputDir(backend)
		dst = os.path.join(destdir, imgfile)
		shutil.copy(src, dst)

		verbose("Saved image to %s" % dst)
		return dst

class Role(Configurable):
	info_attrs = ["name", "platform"]

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
	info_attrs = ["name", "role", "platform"]

	def __init__(self, name):
		self.name = name
		self.role = name
		self.platform = None
		self.build = None
		self.install = []
		self.start = []
		self._backends = BackendDict()

	def configure(self, config):
		if not config:
			return

		self._backends.configure(config)
		self.update_value(config, 'role')
		self.update_value(config, 'build')
		self.update_value(config, 'platform')
		self.update_list(config, 'install')
		self.update_list(config, 'start')

class Build(Platform):
	info_attrs = Platform.info_attrs + ['base_platform']

	def __init__(self, name):
		super().__init__(name)
		self.base_platform = None
		self.template = None
		self.backend_build_config = None

	def configure(self, config):
		super().configure(config)
		self.update_value(config, 'base_platform', 'base-platform')
		self.update_value(config, 'template')

	def mergeBackendConfigs(self, backendConfigs):
		if not backendConfigs.configs:
			return

		saved = self.backends.create(backendConfigs.name)
		saved.configs = backendConfigs.configs + saved.configs

	def resolveImage(self, config, backend, base_os = None, arch = None):
		assert(type(backend) == str)
		if not self.base_platform:
			return None

		base_platform = config.getPlatform(self.base_platform)
		if base_platform is None:
			raise ConfigError("Cannot find base platform \"%s\"" % self.base_platform)

		if not base_platform.imagesets:
			return None

		if arch is None:
			arch = os.uname().machine

		found = None
		for imageSet in base_platform.imagesets.values():
			if base_os and imageSet.os != base_os:
				continue

			arch_specific = imageSet.getArchitecture(arch)
			if not arch_specific:
				continue

			build_config = arch_specific.getBackend(backend)
			if not build_config:
				continue

			if found:
				verbose("Found more than one matching image in base platform %s" % self.base_platform)
				return None

			found = imageSet

		if found:
			self.features += base_platform.features
			self.requires += base_platform.requires
			if not self.vendor:
				self.vendor = base_platform.vendor
			if not self.os:
				self.os = base_platform.os
			if not self.arch:
				self.arch = arch

			self.mergeBackendConfigs(build_config)
		else:
			verbose("No matching image in base platform %s" % self.base_platform)

		return found

	def describeBuildResult(self, name = None):
		if name is None:
			name = self.name

		result = Platform(name)
		result.vendor = self.vendor
		result.os = self.os
		result.features = self.features
		result.repositories = self.repositories

		return result

class EmptyNodeConfig:
	def __init__(self, name):
		self.name = name
		self.role = None
		self.platform = None
		self.repositories = []
		self.install = []
		self.start = []
		self.requires = []
		self.features = []
		self.backends = BackendDict()
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
		self.requires += platform.requires
		self.backends = node._backends
		self.info = global_info

		if isinstance(platform, Build):
			self.buildResult = platform.describeBuildResult()
		else:
			self.buildResult = None

	# Called from the backend when it detects a new private key
	# during provisioning.
	# Currently, only used while building a new silver image, in
	# which case we push the raw key to the buildResult,
	# which stores its binary data in some attribute.
	#
	# Later, during save(), it writes out the actual raw data.
	def captureKey(self, path):
		# If we're not building anything, there's no point in
		# capturing the ssh key
		if self.buildResult is None:
			return

		with open(path, "rb") as f:
			self.buildResult.setRawKey(f.read())

class Config(Configurable):
	_default_config_dirs = [
		# This is defines in _twopence/paths.py
		twopence_global_config_dir,
	]

	def __init__(self, workspace):
		self.workspace = workspace
		self.logspace = None
		self.testcase = None
		self.status = None
		self._user_config_dirs = []

		self._backends = BackendDict()
		self._platforms = ConfigDict("platform", Platform, verbose = True)
		self._roles = ConfigDict("role", Role, verbose = True)
		self._nodes = ConfigDict("node", Node, verbose = True)
		self._builds = ConfigDict("build", Build, verbose = True)
		self._repositories = []

		self.info = ExtraInfo()

		self.defaultRole = self._roles.create("default")

		self._valid = False

	def addDirectory(self, path):
		path = os.path.expanduser(path)
		self._user_config_dirs.append(path)

	# Given a config file name (foo.conf) try to locate the 
	# file in a number of directories.
	# Note that user directories (added by .addDirectory() above) take
	# precedence over the standard ones like /etc/twopence.
	def locateConfig(self, filename):
		for basedir in self._user_config_dirs + Config._default_config_dirs:
			path = os.path.join(basedir, filename)
			if os.path.exists(path):
				return path
		return None

	def load(self, filename):
		filename = self.locateConfig(filename)
		if filename is None:
			return False

		debug("Loading %s" % filename)
		config = curly.Config(filename)

		self.configure(config.tree())
		return True

	def configure(self, tree):
		self._backends.configure(tree)
		self._platforms.configure(tree)
		self._roles.configure(tree)
		self._nodes.configure(tree)
		self._builds.configure(tree)

		self.update_value(tree, 'workspaceRoot', 'workspace-root')
		self.update_value(tree, 'workspace')
		self.update_value(tree, 'testcase')

		# Extract data from global info "blah" { ... } groups
		self.info.configure(tree)

	def validate(self, purpose = None):
		if purpose == "testing":
			if not self.testcase:
				raise ConfigError("no testcase name configured")
			if not self.nodes:
				raise ConfigError("no nodes configured")
		elif purpose == "building":
			if not self.builds:
				raise ConfigError("no builds configured")

		if self._valid:
			return

		if not self.workspace:
			raise ConfigError("no workspace configured")

		self._valid = True

	@property
	def platforms(self):
		return self._platforms.values()

	def getPlatform(self, name):
		found = self._platforms.get(name)
		if found is None:
			if self.load("platform.d/%s.conf" % name):
				found = self._platforms.get(name)
		return found

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

	@property
	def builds(self):
		return self._builds.values()

	def getBuild(self, name):
		return self._builds.get(name)

	def configureBackend(self, backend):
		for config in self._backends.savedConfigs(backend.name):
			backend.configure(config)

	def findBuildNode(self):
		result = None
		for node in self.nodes:
			if node.build:
				if result:
					raise ConfigError("More than one node with a build target; unable to handle")
				result = node

		return result

	def finalizeNode(self, node, backend):
		platform = self.platformForNode(node, backend)

		if not platform.vendor or not platform.os:
			raise ConfigError("Node %s uses platform %s, which lacks a vendor and os definition" % (platform.name, node.name))

		if platform.requires:
			for name in platform.requires:
				if self.load("%s.conf" % name):
					continue

				raise ConfigError("node %s requires \"%s\" but I don't know how to provide it (maybe you need to create %s)" % (
							node.name, name, filename))

		result = FinalNodeConfig(node, platform, self.info)

		role = self.getRole("default")
		if role:
			result.fromRole(role)

		role = self.getRole(node.role)
		if role:
			result.fromRole(role)

		# Extract backend specific config data from node, role and platform
		backend.configureNode(result, self)

		return result

	@staticmethod
	def createEmptyNode(name, workspace = None):
		return EmptyNodeConfig(name)

	def platformForNode(self, node, backend):
		if node.build:
			build = self._builds.get(node.build)
			if not build:
				raise ConfigError("Cannot find build \"%s\" for node \"%s\"" % (node.build, node.name))

			# This locates the correct base image for this build
			if not build.resolveImage(self, backend.name):
				raise ConfigError("Cannot resolve base image \"%s\" for build \"%s\"" % (build.base_platform, build.name))
			return build

		if node.platform:
			platform = self.getPlatform(node.platform)
			if platform:
				return platform

			raise ConfigError("Cannot find platform \"%s\" for node \"%s\"" % (node.platform, node.name))

		role = self.getRole(node.role)
		if role and role.platform:
			platform = self.getPlatform(role.platform)
			if platform:
				return platform

			raise ConfigError("Cannot find platform \"%s\" for role \"%s\"" % (role.platform, node.role))

		if self.defaultRole.platform:
			platform = self.getPlatform(self.defaultRole.platform)
			if platform:
				return platform

			raise ConfigError("Cannot find platform \"%s\" for default role" % (self.defaultRole.platform))

		raise ConfigError("No platform defined for node \"%s\" (role \"%s\")" % (node.name, node.role))
