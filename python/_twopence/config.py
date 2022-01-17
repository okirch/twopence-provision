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
from .provision import ProvisioningScriptCollection, ProvisioningShellEnvironment

class ConfigError(Exception):
	pass

def typeconv_str_to_bool(value):
	if value is None:
		return False
	value = value.lower()
	if value in ('true', 'yes', 'on', '1'):
		return True
	if value in ('false', 'no', 'off', '0'):
		return False
	raise ValueError("Unable to convert \"%s\" to boolean" % value)

##################################################################
# This is a helper class that simplifies how we populate a
# python object from a curly config file.
##################################################################
class Configurable(object):
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

	def add(self, obj):
		assert(isinstance(obj, self.item_class))
		if obj.name in self:
			raise KeyError("Detected duplicate object name %s" % obj.name)
		self[obj.name] = obj

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

#
# A platform definition can describe requirements (such as an activation regcode).
# We want to be able to
#  (a) store these in a curly config file somewhere below ~/.twopence
#  (b) prompt the user for this data if it's not cached somewhere
#  (c) export this information as (shell) variables to the provisioning code
#
# A platform may require a string, such as "suse-registration".
#
# The prompting information is a set of of items, as in
#	requirement "suse-sles-registration" {
#		provides "suse-registration"
#		item regcode {
#			prompt "Please enter regcode";
#		}
#		item email ...
#	}
#
# The "provides" attribute is used so that we can make scripts a bit more
# generic. For instance, different products may require different regcodes,
# but they all provide the same class of information (ie suse-registration).
# The provisioning script doesn't have to understand each regcode, it can
# be written to refer to generic "suse-registration" data.
#
# When caching this information, it will be stored in
# ~/.twopence/config/suse-sles-registration.conf and contain s.th. like this:
#	info "suse-registration" {
#		email "Olaf.Kirch@suse.com";
#		regcode "BLAH-BLAH-BLAH";
#	}
# Note the difference between the file name (which reflects the name of the
# requirement) and the name on the info {} block (which reflects what this
# set of data provides).
#
# This information is provided to provisioning scripts as shell variables:
#  TWOPENCE_INFO_SUSE_REGISTRATION_EMAIL
#  TWOPENCE_INFO_SUSE_REGISTRATION_REGCODE
#
class ConfigRequirement(Configurable):
	info_attrs = ['name', 'provides', 'valid']

	class Item(Configurable):
		def __init__(self, name, config):
			self.name = name
			self.prompt = None
			self.default = None

			self.update_value(config, 'prompt')
			self.update_value(config, 'default')

	class Fnord(Configurable):
		info_attrs = ['name']

		def __init__(self, name, data = None):
			self.name = name
			self.data = data or {}

		def configure(self, config):
			for name in config.get_attributes():
				self.data[name] = config.get_value(name)

		def publish(self, curlyNode):
			for key, value in self.data.items():
				curlyNode.set_value(key, value)

	def __init__(self, name):
		self.name = name
		self.provides = name
		self.valid = []
		self.items = []

		self._cache = None

	def configure(self, config):
		self.update_list(config, 'valid')
		self.update_value(config, 'provides')
		for name in config.get_children("item"):
			item = self.Item(name, config.get_child("item", name))
			self.items.append(item)

	def prompt(self):
		for item in self.items:
			yield item.name, item.prompt, item.default

	def getResponse(self, nodeName):
		return self._cache

	def getCachedResponse(self, nodeName):
		return self._cache

	def loadResponse(self, nodeName, config):
		name = self.name

		if "permanent" not in self.valid:
			return None

		debug(f"Locating requirement {self.name}")
		path = config.locateConfig(f"{name}.conf")
		if path is None:
			debug(f"No cached config for requirement {name}")
			return None

		debug(f"Loading requirement {self.name} from {path}")
		cfg = curly.Config(path)
		child = cfg.tree().get_child("info", self.provides)
		if child is None:
			warning(f"file {path} should contain info {self.provides} " + "{ ... }")
			warning(f"Ignoring {path}...")
			return None

		response = self.Fnord(self.provides)
		response.configure(child)

		return response

	def buildResponse(self, nodeName, data):
		response = self.Fnord(self.provides, data)

		if "allnodes" in self.valid:
			self._cache = response

		self.saveResponse(nodeName, response)
		return response

	def saveResponse(self, nodeName, response):
		if "allnodes" in self.valid:
			self._cache = response

		if "permanent" not in self.valid:
			return

		path = os.path.expanduser(twopence_user_config_dir)
		path = os.path.join(path, f"{self.name}.conf")

		debug(f"Saving requirement {self.name} to {path}")
		cfg = curly.Config()

		root = cfg.tree()
		child = root.add_child("info", self.provides)
		response.publish(child)

		cfg.save(path)

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

	def merge(self, other):
		assert(isinstance(other, BackendDict))
		for be in other.values():
			self.mergeSavedConfig(be)

	def mergeSavedConfig(self, other):
		assert(isinstance(other, SavedBackendConfig))
		if not other.configs:
			return

		saved = self.create(other.name)
		saved.configs = other.configs + saved.configs

class Repository(Configurable):
	info_attrs = ['name', 'url']

	def __init__(self, name):
		self.name = name
		self.url = None
		self.keyfile = None
		self.enabled = False
		self.active = False

	def configure(self, config):
		if not config:
			return

		self.update_value(config, 'url')
		self.update_value(config, 'keyfile')
		self.update_value(config, 'enabled', typeconv = typeconv_str_to_bool)
		self.update_value(config, 'active', typeconv = typeconv_str_to_bool)

	def publish(self, config):
		if self.url:
			config.set_value("url", self.url)
		if self.keyfile:
			config.set_value("keyfile", self.keyfile)
		config.set_value("enabled", str(self.enabled))
		config.set_value("active", str(self.active))

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

class BuildStage(Configurable):
	info_attrs = ['name', 'reboot', 'run', 'only']

	defaultOrder = {
		'prep'		: 0,
		'install'	: 1,
		'provision'	: 2,
		'build'		: 10,
		'other'		: 50,
		'cleanup'	: 100,
	}
	defaultCategory = {
		'prep'		: 'prep',
		'install'	: 'prep',
		'provision'	: 'prep',
		'build'		: 'build',
		'cleanup'	: 'cleanup',
	}

	def __init__(self, name, category = None, order = None):
		self.name = name
		self.run = []
		self.commands = []
		self.only = None
		self.reboot = False

		if category is None:
			category = self.defaultCategory.get(name)
		if category is None:
			category = "other"
		self.category = category

		if order is None:
			order = self.defaultOrder.get(self.name)
		if order is None:
			order = self.defaultOrder.get(self.category)
		if order is None:
			order = 50
		self.order = order

	def zap(self):
		self.run = []
		self.reboot = False

	def configure(self, config):
		self.update_list(config, 'run')
		self.update_value(config, 'order', typeconv = int)
		self.update_value(config, 'reboot', typeconv = typeconv_str_to_bool)
		self.update_value(config, 'only')

		self.validate()

	def publish(self, config):
		if self.run:
			config.set_value("run", self.run)
		if self.order:
			config.set_value("order", str(self.order))
		config.set_value("reboot", str(self.reboot))
		if self.only:
			config.set_value("only", self.only)

	def merge(self, other, insert = False):
		assert(isinstance(other, BuildStage))
		if insert:
			self.run = other.run + self.run
		else:
			self.run = self.run + other.run
		self.reboot = self.reboot or other.reboot

	def validate(self):
		for path in self.paths():
			if not os.path.isfile(path):
				raise ConfigError("Script snippet \"%s\" does not exist" % path)

	def load(self):
		result = []
		for path in self.paths():
			debug("Trying to load script snippet from %s" % path)

			result += ["", "# BEGIN %s" % path]
			with open(path, "r") as f:
				result += f.read().split('\n')
				result.append("# END OF %s" % path)

		result += self.commands

		return result

	def paths(self):
		result = []

		stagedir = os.path.join("/usr/lib/twopence/provision", self.category)
		for name in self.run:
			path = os.path.join(stagedir, name)
			result.append(path)

		return result

class Platform(Configurable):
	info_attrs = ['name', 'image', 'vendor', 'os', 'imagesets', 'requires', 'features', 'install', 'start']

	def __init__(self, name):
		self.name = name
		self.arch = None
		self.image = None
		self.keyfile = None
		self.repositories = ConfigDict("repository", Repository)
		self.imagesets = ConfigDict("imageset", Imageset)
		self.stages = ConfigDict("stage", BuildStage)
		self.backends = BackendDict()
		self.install = []
		self.start = []
		self.requires = []
		self.features = []
		self.vendor = None
		self.os = None
		self.build_time = None

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
		self.update_list(config, 'install')
		self.update_list(config, 'start')
		self.update_value(config, 'vendor')
		self.update_value(config, 'os')
		self.update_value(config, 'build_time', 'build-time')

		self.repositories.configure(config)
		self.imagesets.configure(config)
		self.backends.configure(config)
		self.stages.configure(config)

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
		if self.build_time:
			config.set_value('build-time', self.build_time)

		self.repositories.publish(config)
		self.stages.publish(config)

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

	def describeBuildResult(self, name = None):
		if name is None:
			name = self.name

		result = Platform(name)
		result.vendor = self.vendor
		result.os = self.os
		result.features = self.features
		result.repositories = self.repositories

		return result

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

	# We need to deal with two cases here
	# a) the platform defines an image directly
	#	backend vagrant {
	#		image "blah";
	#	}
	# b) the platform defines an image set that we need to choose from
	#	imageset "Leap-15.3" {
	#		architecture x86_64 {
	#			backend vagrant {
	#				image		"blah";
	#			}
	#		}
	#	}
	def resolveImage(self, config, backend, base_os = None, arch = None):
		assert(type(backend) == str)

		for saved in self.backends.savedConfigs(backend):
			if saved.get_value("image") is not None:
				return True

		if not self.imagesets:
			return False

		if arch is None:
			arch = os.uname().machine

		found = None
		for imageSet in self.imagesets.values():
			if base_os and imageSet.os != base_os:
				continue

			arch_specific = imageSet.getArchitecture(arch)
			if not arch_specific:
				continue

			build_config = arch_specific.getBackend(backend)
			if not build_config:
				continue

			if found:
				verbose("Found more than one matching image in base platform %s" % self)
				return False

			found = imageSet

		if found is None:
			verbose("No matching image in platform %s" % self)
			return False

		self.mergeBackendConfigs(build_config)
		self.arch = arch
		return True

	def mergeBackendConfigs(self, backendConfigs):
		if not backendConfigs.configs:
			return

		saved = self.backends.create(backendConfigs.name)
		saved.configs = backendConfigs.configs + saved.configs

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
	info_attrs = ["name", "role", "platform", "build"]

	def __init__(self, name):
		self.name = name
		self.role = name
		self.platform = None
		self.build = []
		self.install = []
		self.start = []
		self._backends = BackendDict()

	def configure(self, config):
		if not config:
			return

		self._backends.configure(config)
		self.update_value(config, 'role')
		self.update_list(config, 'build')
		self.update_value(config, 'platform')
		self.update_list(config, 'install')
		self.update_list(config, 'start')

class Build(Platform):
	info_attrs = Platform.info_attrs + ['base_platform']

	def __init__(self, name):
		super().__init__(name)
		self.base_platform = None
		self.template = None

	def configure(self, config):
		super().configure(config)
		self.update_value(config, 'base_platform', 'base-platform')
		self.update_value(config, 'template')

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
		self.satisfiedRequirements = None
		self._stages = {}

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

	@property
	def stages(self):
		return sorted(self._stages.values(), key = lambda stage: stage.order)

	def createStage(self, name):
		stage = self._stages.get(name)
		if stage is None:
			stage = BuildStage(name)
			self._stages[name] = stage
		return stage

	def mergeStage(self, stage):
		mine = self.createStage(stage.name)
		mine.merge(stage)

	def cookedStages(self):
		return ProvisioningScriptCollection(self.stages, self.exportShellVariables())

	def exportShellVariables(self):
		debug("Building shell variables for node %s" % self.name)
		result = ProvisioningShellEnvironment()
		result.export("TWOPENCE_HOSTNAME", self.name)
		result.export("TWOPENCE_PLATFORM", self.platform.name)
		result.export("TWOPENCE_VENDOR", self.platform.vendor)
		result.export("TWOPENCE_OS", self.platform.os)
		result.export("TWOPENCE_ARCH", self.platform.arch)
		result.export("TWOPENCE_FEATURES", self.features)
		result.export("TWOPENCE_INSTALL_PACKAGES", self.install)
		result.export("TWOPENCE_START_SERVICES", self.start)

		activate_repos = []
		for repo in self.repositories:
			if repo.active:
				print("Repository %s already active; no need to activate it" % repo.name)
				continue

			name = repo.name
			result.export("TWOPENCE_REPO_%s_URL" % name, repo.url)

			keyfile = repo.keyfile
			if keyfile is None:
				keyfile = "%s/repodata/repomd.xml.key" % repo.url

			if not keyfile.startswith("http:") and not keyfile.startswith("https:"):
				warning("Repository %s specifies keyfile %s - this will most likely fail" % (repo.name, keyfile))
			else:
				result.export("TWOPENCE_REPO_%s_KEY" % name, keyfile)

			activate_repos.append(name)

			# When we build a silver image, the definition for this repo is written
			# to the platform config file - but marked as "active". When we then
			# provision a machine with this image, the flag tells us that we do not
			# have to activate it again (see a few above)
			repo.active = True

		result.export("TWOPENCE_ADD_REPOSITORIES", activate_repos)

		for response in self.satisfiedRequirements:
			respName = response.name.replace('-', '_')
			prefix = f"TWOPENCE_INFO_{respName}"
			result.exportDict(response.data, prefix)

		return result

class FinalNodeConfig(EmptyNodeConfig):
	def __init__(self, node, platform, build_options, satisfied_requirements):
		super().__init__(node.name)

		self.platform = platform
		self.install += node.install
		self.start += node.start
		self.backends = node._backends
		self.satisfiedRequirements = satisfied_requirements

		self.describeBuildResult()

		self.mergePlatformOrBuild(platform)
		for build in build_options:
			self.mergePlatformOrBuild(build)

			# override any backend specific settings from the build
			# option
			self.backends.merge(build.backends)

		for stage in platform.stages.values():
			# stage.only can be one of
			#  build: only applicable during build; so don't publish to resulting image
			#  once: only execute once; don't publish to resulting image either
			#
			if stage.only is None:
				self.buildResult.stages.add(stage)

	def mergePlatformOrBuild(self, p):
		self.features += p.features
		self.install += p.install
		self.start += p.start
		self.requires += p.requires

		for stage in p.stages.values():
			if stage.only == 'build' and self.name != 'build':
				print("Skipping stage %s (marked as %s only)" % (stage.name, stage.only))
				continue

			self.mergeStage(stage)

		# Loop over all specified repos. If a repo is marked with
		# "enabled = True", we enable it right away.
		for repo in p.repositories.values():
			if repo.enabled and repo not in self.repositories:
				self.repositories.append(repo)

			self.buildResult.repositories.add(repo)

		self.buildResult.features += p.features

	def describeBuildResult(self):
		base = self.platform

		result = Platform(base.name)
		result.vendor = base.vendor
		result.os = base.os

		self.buildResult = result
		return result

	def display(self):
		print("Node %s" % self.name)
		print("  Platform   %s" % self.platform)
		print("  Install    %s" % self.install)
		print("  Start      %s" % self.start)
		print("  Features   %s" % self.features)
		print("  Requires   %s" % self.requires)
		for stage in self.stages:
			print("   stage %s" % stage)

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
		self._requirementsManager = None
		self._user_config_dirs = []

		self._backends = BackendDict()
		self._platforms = ConfigDict("platform", Platform, verbose = True)
		self._roles = ConfigDict("role", Role, verbose = True)
		self._nodes = ConfigDict("node", Node, verbose = True)
		self._builds = ConfigDict("build", Build, verbose = True)
		self._requirements = ConfigDict("requirement", ConfigRequirement, verbose = True)
		self._repositories = []
		self._parameters = {}

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

	class PlatformInfo(Configurable):
		info_attrs = ['path']

		def __init__(self, path):
			self.path = path

			self._platforms = ConfigDict("platform", Platform, verbose = True)
			self._builds = ConfigDict("build", Build, verbose = True)
			self.build_time = None

			config = curly.Config(path)
			self.configure(config.tree())

		def configure(self, tree):
			self._platforms.configure(tree)
			self._builds.configure(tree)
			self.update_value(tree, 'build_time', 'build-time')

		@property
		def builds(self):
			return self._builds.values()

		@property
		def platforms(self):
			return self._platforms.values()

	def locatePlatformFiles(self):
		for basedir in self._user_config_dirs + Config._default_config_dirs:
			path = os.path.join(basedir, "platform.d")
			if os.path.isdir(path):
				for de in os.scandir(path):
					if not de.is_file() or not de.name.endswith(".conf"):
						continue

					yield self.PlatformInfo(de.path)

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
		self._requirements.configure(tree)

		self.update_value(tree, 'workspaceRoot', 'workspace-root')
		self.update_value(tree, 'workspace')
		self.update_value(tree, 'testcase')

		# commonly, parameters are defined in testrun.conf, but
		# the may also come from any other config file.
		child = tree.get_child("parameters")
		if child:
			for name in child.get_attributes():
				self._parameters[name] = child.get_value(name)

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

	@property
	def parameters(self):
		return self._parameters

	@parameters.setter
	def parameters(self, value):
		assert(isinstance(value, dict))
		self._parameters.update(value)

	@property
	def requirements(self):
		return self._requirements

	def getRequirement(self, name):
		return self._requirements.get(name)

	@property
	def requirementsManager(self):
		return self._requirementsManager

	@requirementsManager.setter
	def requirementsManager(self, value):
		assert(isinstance(value, RequirementsManager))
		self._requirementsManager = value

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
		if not platform.resolveImage(self, backend.name):
			raise ConfigError("Unable to determine image for node %s" % node.name)

		build_options = []
		for name in node.build:
			build = self.getBuild(name)
			if build is None:
				raise ConfigError("Node %s wants to use feature %s, but I can't find it" % (node.name, name))
			build_options.append(build)

		if not platform.vendor or not platform.os:
			raise ConfigError("Node %s uses platform %s, which lacks a vendor and os definition" % (platform.name, node.name))

		satisfied = []
		if platform.requires:
			for name in platform.requires:
				response = None
				if self._requirementsManager:
					response = self._requirementsManager.handle(node.name, name)

				if response is None:
					raise ConfigError("node %s requires \"%s\" but I don't know how to provide it" % (node.name, name))

				satisfied.append(response)

		result = FinalNodeConfig(node, platform, build_options, satisfied)

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

	def createNode(self, name):
		return self._nodes.create(name)

	def platformForNode(self, node, backend):
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

##################################################################
# Handle requirements
# Front-end should derive from this
##################################################################
class RequirementsManager(object):
	def __init__(self, config):
		self.config = config
		self._cache = dict()
		self._configs = []

	# This should be implemented by subclasses
	# It should return a dict mapping item name to value
	def prompt(self, nodeName, req):
		return None

	def handle(self, nodeName, reqName):
		req = self.config.getRequirement(reqName)
		if req is None:
			raise ConfigError("Nothing known about requirement %s" % reqName)

		# First, let's see if we cached it during a previous call
		response = req.getCachedResponse(nodeName)

		if response is None:
			response = req.loadResponse(nodeName, self.config)

		if response is None:
			data = self.prompt(nodeName, req)
			if data:
				response = req.buildResponse(nodeName, data)

		return response
