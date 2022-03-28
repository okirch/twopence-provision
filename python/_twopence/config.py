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
import copy
import twopence

from twopence import ConfigError
from twopence.schema import *
from .logging import *
from .provision import ProvisioningScriptCollection, ProvisioningShellEnvironment, ProvisioningFile

class Compatibility(NamedConfigurable):
	info_attrs = ['requires', 'conflicts']

	schema = [
		SetAttributeSchema('requires'),
		SetAttributeSchema('conflicts'),
	]

	def check(self, values, name, category):
		okay = True

		conflict = self.conflicts.intersection(values)
		if conflict:
			error(f"{name} conflicts with {category}s " + ", ".join(conflict))
			okay = False

		require = self.requires.difference(values)
		if require:
			error(f"{name} requires missing {category}s " + ", ".join(require))
			okay = False

		return okay


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
class ConfigRequirement(NamedConfigurable):
	info_attrs = ['name', 'provides', 'valid']

	class Item(NamedConfigurable):
		schema = [
			StringAttributeSchema('prompt'),
			StringAttributeSchema('default'),
		]

	schema = [
		StringAttributeSchema('provides'),
		ListAttributeSchema('valid'),
		ListNodeSchema('_items', 'item', itemClass = Item),
	]

	def __init__(self, name):
		super().__init__(name)
		self._cache = None

	@property
	def items(self):
		return iter(self._items)

	def prompt(self):
		for item in self.items:
			yield item.name, item.prompt, item.default

	def getResponse(self, nodeName):
		return self._cache

	def getCachedResponse(self, nodeName):
		return self._cache

	def loadResponse(self, nodeName, catalog):
		name = self.name

		if "permanent" not in self.valid:
			return None

		debug(f"Locating requirement {self.name}")
		file = catalog.locateFileByName(name)
		if file is None:
			debug(f"No cached config for requirement {name}")
			return None

		response = file.getResponse(self.provides)
		if response is None:
			warning(f"file {path} should contain info {self.provides} " + "{ ... }")
			warning(f"Ignoring {path}...")
			return None

		return response

	def buildResponse(self, nodeName, data):
		response = ConfigOpaque(self.provides, data)

		if "allnodes" in self.valid:
			self._cache = response

		return response

	def shouldSaveResponse(self, response):
		return response and "permanent" in self.valid

class Repository(NamedConfigurable):
	info_attrs = ['name', 'url']

	schema = [
		StringAttributeSchema('url'),
		StringAttributeSchema('keyfile'),
		BooleanAttributeSchema('enabled'),

		StringAttributeSchema('x_zypp_vendor', key = 'x-zypp-vendor'),
	]

class Imageset(NamedConfigurable):
	info_attrs = ['name']

	class Architecture(NamedConfigurable):
		schema = [
			DictNodeSchema('backends', 'backend', itemClass = ConfigOpaque),
		]

		def __str__(self):
			return "Imageset.Arch(%s)" % self.name

		def getBackend(self, name):
			return self.backends.get(name)

	schema = [
		DictNodeSchema('architectures', 'architecture', itemClass = Architecture),
	]

	def getArchitecture(self, name):
		return self.architectures.get(name)

class BuildInvocation:
	def __init__(self, string):
		self.action = None
		self.path = None
		self.command = None
		self._arguments = ""

		w = string.split(maxsplit = 1)
		self.name = w.pop(0)
		if w:
			self._arguments = w.pop(0)

	def __str__(self):
		return f"{self.name} {self._arguments}"

	def resolve(self, actionLibrary):
		action = actionLibrary.get(self.name)
		if action is None:
			raise ConfigError(f"Provisioning stage refers unknown action {self.name}")

		self.action = action
		if action.command:
			self.command = f"{action.command} {self._arguments}"
			self.path = None
		elif action.function:
			self.command = f"{action.function} {self._arguments}"
			self.path = action.path
		else:
			raise ConfigError(f"Action {self.name} does not specify command or function")

	def files(self):
		if self.path is None:
			return []

		debug(f"path for {self} is {self.path}")
		return [ProvisioningFile(self.path)]

	def commands(self):
		return ["",
			f"# Expanded from {self.name}",
			f"twopence_exec {self.command}"]

class BuildStage(NamedConfigurable):
	info_attrs = ['name', 'reboot', 'run', 'only']

	defaultOrder = {
		'preamble'		: 0,

		'prep'			: 5,
		'install'		: 6,
		'provision'		: 7,

		# built-in stages
		'add-repositories'	: 10,
		'install-packages'	: 11,
		'start-services'	: 12,

		'build'			: 20,
		'other'			: 50,
		'cleanup'		: 100,
	}
	# this is too convoluted
	defaultCategory = {
		'preamble'		: 'prep',
		'prep'			: 'prep',
		'install'		: 'prep',
		'provision'		: 'prep',
		'build'			: 'build',
		'cleanup'		: 'cleanup',
	}

	schema = [
		StringAttributeSchema('only'),
		ListAttributeSchema('run'),
		ListAttributeSchema('perform'),
		IntegerAttributeSchema('order'),
		BooleanAttributeSchema('reboot'),
	]

	def __init__(self, name, category = None, order = None):
		super().__init__(name)

		# What is this for?
		self.commands = []

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

		self.invocations = []

	@property
	def is_empty(self):
		return not(self.run or self.perform)

	def zap(self):
		self.run = []
		self.perform = []
		self.reboot = False

	def configure(self, config):
		super().configure(config)
		self.validate()

	def validate(self):
		for file in self.files():
			if not os.path.isfile(file.path):
				raise ConfigError("Script snippet \"%s\" does not exist" % file.path)

		self.invocations = []
		for cmd in self.perform:
			if cmd:
				self.addInvocation(cmd)

	def addInvocation(self, cmd):
		# debug(f"Action {self.name}: add invocation {cmd}")
		invocation = BuildInvocation(cmd)
		self.invocations.append(invocation)

	# A stage can execute individual actions, which often refer to a
	# script snippet that defines one or more shell functions
	# We resolve these action names once the platform definition is
	# complete.
	def resolveActions(self, actionLibrary):
		if self.invocations:
			# debug(f"{self} resolving actions");
			for invocation in self.invocations:
				# debug(f"  {invocation.name}")
				invocation.resolve(actionLibrary)
				if invocation.path:
					# debug(f"    uses {invocation.path}")
					pass

	def merge(self, other, insert = False):
		assert(isinstance(other, BuildStage))
		if insert:
			self.run = other.run + self.run
			self.perform = other.perform + self.perform
			self.invocations = other.invocations + self.invocations
		else:
			self.run = self.run + other.run
			self.perform = self.perform + other.perform
			self.invocations = self.invocations + other.invocations
		self.reboot = self.reboot or other.reboot

	def files(self):
		result = []
		for name in self.run:
			result.append(ProvisioningFile(self.category, name))

		for invocation in self.invocations:
			result += invocation.files()

		return result

	def shellCommands(self):
		result = [] + self.commands
		for invocation in self.invocations:
			result += invocation.commands()
		return result

class Action(NamedConfigurable):
	info_attrs = ['name']


class ShellAction(Action):
	schema = [
		StringAttributeSchema('script'),
		StringAttributeSchema('function'),
		StringAttributeSchema('command'),
	]

	def __init__(self, name):
		super().__init__(name)
		self.arguments = []

	@property
	def path(self):
		return f"shell/{self.script}"

	def files(self):
		if self.path is None:
			return []

		debug(f"path for {self} is {self.script}")
		return [ProvisioningFile("shell", self.script)]


	@staticmethod
	def defaultPreamble():
		action = ShellAction("preamble")
		action.script = "preamble"
		return action

class Platform(NamedConfigurable):
	info_attrs = ['name', 'image', 'vendor', 'os', 'imagesets', 'requires',
			'repositories',
			'features', 'install', 'start', 'resources']

	schema = [
		StringAttributeSchema('vendor'),
		StringAttributeSchema('os'),
		StringAttributeSchema('arch'),
		StringAttributeSchema('image'),		# obsolete?
		SetAttributeSchema('_features', 'features'),
		SetAttributeSchema('_non_features', 'non-features'),
		ListAttributeSchema('resources'),
		ListAttributeSchema('requires'),
		ListAttributeSchema('_base_platforms', 'use-base-platforms'),
		StringAttributeSchema('keyfile', 'ssh-keyfile'),
		StringAttributeSchema('build_time', 'build-time'),
		StringAttributeSchema('built_from', 'built-from'),
		ListAttributeSchema('install'),
		ListAttributeSchema('start'),
		ListAttributeSchema('_always_build', 'always-build'),
		ListAttributeSchema('_active_repositories', 'active-repositories'),
		ListAttributeSchema('_applied_stages', 'applied-stages'),
		SetAttributeSchema('_applied_build_options', 'applied-build-options'),

		DictNodeSchema('repositories', 'repository', itemClass = Repository),
		DictNodeSchema('imagesets', 'imageset', itemClass = Imageset),
		DictNodeSchema('stages', 'stage', itemClass = BuildStage),
		DictNodeSchema('backends', 'backend', itemClass = ConfigOpaque),
		DictNodeSchema('shellActions', 'shell', itemClass = ShellAction),
	]

	def __init__(self, name):
		super().__init__(name)

		self.base_platforms = None

	def getRepository(self, name):
		return self.repositories.get(name)

	def searchRepository(self, name):
		search = [self]
		while search:
			platform = search.pop(0)
			repo = platform.getRepository(name)
			if repo is not None:
				return repo

			search += platform.base_platforms
		return None

	def repositoryIsActive(self, repo):
		return repo.name in self._active_repositories

	def repositoryMarkActive(self, repo):
		if repo.name not in self._active_repositories:
			self._active_repositories.append(repo.name)

	@property
	def applied_build_options(self):
		return self._applied_build_options

	@property
	def features(self):
		return self._features.difference(self._non_features)

	def updateFeatures(self, features):
		self._features.update(features.difference(self._non_features))

	@property
	def isApplication(self):
		return False

	##########################################################
	# The remaining methods and properties are for newly
	# built silver images only
	##########################################################
	def addBackend(self, name, **kwargs):
		saved = self.backends.create(name)
		for key, value in kwargs.items():
			saved.set_value(key, value)

	# FIXME obsolete?
	def hasBackend(self, name):
		return self.backends.get(name) is not None

	# FIXME obsolete
	def finalize(self):
		pass

	def save(self):
		new_config = curly.Config()

		config = new_config.tree()
		child = config.add_child("platform", self.name)
		self.publish(child)

		path = os.path.join(self.platformdir, "%s.conf" % self.name)
		new_config.save(path)
		verbose("Saved platform config to %s" % path)

	def getOutputDir(self, name):
		path = os.path.expanduser(twopence.user_data_dir)
		path = os.path.join(path, name)
		if not os.path.isdir(path):
			os.makedirs(path)
		return path

	def getImagePath(self, backend, imgfile):
		destdir = self.getOutputDir(backend)
		return os.path.join(destdir, imgfile)

	@property
	def datadir(self):
		path = os.path.expanduser(twopence.user_data_dir)
		path = os.path.join(path, self.name)
		if not os.path.isdir(path):
			os.makedirs(path)
		return path

	@property
	def platformdir(self):
		path = os.path.expanduser(twopence.user_config_dir)
		path = os.path.join(path, "platform.d")
		if not os.path.isdir(path):
			os.makedirs(path)
		return path

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
	def resolveImage(self, backend, base_os = None, arch = None):
		buildConfig = self.findValidImageConfig(backend, base_os, arch)
		if buildConfig is not None:
			self.backends.mergeItem(buildConfig)
			self.arch = arch or os.uname().machine
		return buildConfig

	def hasImageFor(self, backend, arch = None):
		return self.findValidImageConfig(backend, None, arch)

	def findValidImageConfig(self, backend, base_os = None, arch = None):
		# debug(f"{self.name}: find image for backend {backend}, arch {arch}, baseOS {base_os}")
		assert(type(backend) == str)

		buildConfig = self.backends.get(backend)
		if buildConfig and buildConfig.get_value("image") is not None:
			return buildConfig

		if not self.imagesets:
			return None

		if arch is None:
			arch = os.uname().machine

		found = None
		for imageSet in self.imagesets.values():
			if base_os and imageSet.os != base_os:
				continue

			arch_specific = imageSet.getArchitecture(arch)
			if not arch_specific:
				continue

			buildConfig = arch_specific.getBackend(backend)
			if not buildConfig:
				continue

			if found:
				error(f"Found more than one matching image in base platform {self}")
				return None

			found = buildConfig

		if found is None:
			debug(f"  no image matching {backend} and {arch}")
		return found

class Role(NamedConfigurable):
	info_attrs = ["name", "platform", "repositories", "features",]

	schema = [
		StringAttributeSchema('platform'),
		StringAttributeSchema('application'),
		ListAttributeSchema('repositories'),
		ListAttributeSchema('install'),
		ListAttributeSchema('start'),
		SetAttributeSchema('features'),
		ListAttributeSchema('build'),
	]

class Node(NamedConfigurable):
	info_attrs = ["name", "role", "platform", "build"]

	schema = [
		StringAttributeSchema('_role', 'role'),
		StringAttributeSchema('platform'),
		StringAttributeSchema('application'),
		ListAttributeSchema('build'),
		ListAttributeSchema('install'),
		ListAttributeSchema('start'),
		DictNodeSchema("_backends", "backend", itemClass = ConfigOpaque),
	]

	@property
	def role(self):
		return self._role or self.name

class Build(Platform):
	info_attrs = Platform.info_attrs + ['base_platform']

	schema = Platform.schema + [
		# currently not being used:
		StringAttributeSchema('base_platform', key = 'base-platform'),
		ListAttributeSchema('_base_builds', 'use-base-builds'),
		DictNodeSchema('_compatibility', 'compatibility', itemClass = Compatibility),
	]

	def __init__(self, name):
		super().__init__(name)
		self.base_builds = None

	def compatibleWithPlatform(self, platform):
		return self.checkPlatformFeatures(set(platform.features))

	def checkPlatformFeatures(self, featureSet):
		okay = True

		compatibility = self._compatibility.get('features')
		if compatibility and not compatibility.check(featureSet, self.name, "platform feature"):
			okay = False

		for baseBuild in self.base_builds:
			if not baseBuild.checkPlatformFeatures(featureSet):
				okay = False

		return okay

class Application(Platform):
#	info_attrs = Platform.info_attrs + ['base_platform']

	schema = Platform.schema + [
		StringAttributeSchema('id'),
		StringAttributeSchema('application_class', 'application-class'),
		SingleNodeSchema('application_resources', 'application-resources', itemClass = ConfigOpaque),
	]

	def __init__(self, name):
		super().__init__(name)

	@property
	def isApplication(self):
		return True

class EmptyNodeConfig:
	def __init__(self, name):
		self.name = name
		self.role = None
		self.platform = None
		self.repositories = []
		self.activate_repositories = []
		self.install = []
		self.start = []
		self.requires = []
		self.features = set()
		self.resources = []
		self.backends = ConfigDict(ConfigOpaque)
		self.satisfiedRequirements = None
		self._stages = {}
		self._shellActions = {}
		self._provisioning = None
		self.requestedBuildOptions = []

	# FIXME: unused?
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
			# note the use of searchRespository(). This does not just look at the
			# repos defined in self.platform itself, but any of its base
			# platforms, too
			repo = self.platform.searchRepository(name)
			if repo is None:
				raise ConfigError("instance %s wants to use repository %s, but platform %s does not define it" % (
							self.name, name, self.platform.name))

			if repo not in self.repositories:
				self.repositories.append(repo)

		self.install += role.install
		self.start += role.start
		self.features.update(role.features)
		self.requestedBuildOptions += role.build

	def configureBackend(self, backendName, backendNode):
		config = self.platform.backends.get(backendName)
		if config is not None:
			backendNode.configure(config)
		config = self.backends.get(backendName)
		if config is not None:
			backendNode.configure(config)

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
		if self._provisioning is None:
			self._provisioning = self.buildProvisioning()
		return self._provisioning

	def buildProvisioning(self):
		self.buildGenericStage("install-packages", self.install, action = "install-package")
		self.buildGenericStage("start-services", self.start, action = "start-service")
		self.buildGenericStage("add-repositories",
						self.activate_repositories,
						actionFunc = lambda repo:
							f"install-repository {repo.url} {repo.name} --key='{repo.keyfile}' --vendor='{repo.x_zypp_vendor}'"
							# this is getting ugly
					)

		for stage in self.stages:
			stage.resolveActions(self._shellActions)

		preambleAction = self._shellActions.get("preamble")
		if preambleAction is None:
			preambleAction = ShellAction.defaultPreamble()
		preamble = []
		for file in preambleAction.files():
			preamble += file.load()

		return ProvisioningScriptCollection(self.stages, self.exportShellVariables(), preamble = preamble)

	def buildGenericStage(self, stageName, list, action = None, actionFunc = None):
		if not list:
			return

		stage = self.createStage(stageName)
		stage.zap()

		if actionFunc is None:
			actionFunc = lambda name: f"{action} {name}"

		# weed out duplicates
		for item in dict.fromkeys(list):
			stage.addInvocation(actionFunc(item))

	def exportShellVariables(self):
		debug("Building shell variables for node %s" % self.name)
		result = ProvisioningShellEnvironment()
		result.export("TWOPENCE_HOSTNAME", self.name)
		result.export("TWOPENCE_PLATFORM", self.platform.name)
		result.export("TWOPENCE_VENDOR", self.platform.vendor)
		result.export("TWOPENCE_OS", self.platform.os)
		result.export("TWOPENCE_ARCH", self.platform.arch)
		result.export("TWOPENCE_FEATURES", self.features)

		for repo in self.activate_repositories:
			name = repo.name
			result.export("TWOPENCE_REPO_%s_URL" % name, repo.url)

			keyfile = repo.keyfile
			if keyfile is None:
				keyfile = "%s/repodata/repomd.xml.key" % repo.url

			if not keyfile.startswith("http:") and not keyfile.startswith("https:"):
				warning("Repository %s specifies keyfile %s - this will most likely fail" % (repo.name, keyfile))
			else:
				result.export("TWOPENCE_REPO_%s_KEY" % name, keyfile)

			if repo.x_zypp_vendor:
				result.export("TWOPENCE_REPO_%s_ZYPP_VENDOR" % name, repo.x_zypp_vendor)

			# When we build a silver image, the definition for this repo is written
			# to the platform config file - but marked as "active". When we then
			# provision a machine with this image, the flag tells us that we do not
			# have to activate it again
			if self.buildResult:
				self.buildResult.repositoryMarkActive(repo)

		result.export("TWOPENCE_ADD_REPOSITORIES",
				list(repo.name for repo in self.activate_repositories))

		for response in self.satisfiedRequirements:
			respName = response.name.replace('-', '_')
			for key, value in response.items():
				varName = f"TWOPENCE_INFO_{respName}_{key}".upper()
				result.export(varName, value)

		return result

class FinalNodeConfig(EmptyNodeConfig):
	def __init__(self, node, buildContext, roles):
		super().__init__(node.name)

		self.buildContext = buildContext
		self.platform = buildContext.platform
		self.role = node.role
		self.install += node.install
		self.start += node.start
		self.backends = node._backends
		self.satisfiedRequirements = buildContext.satisfiedRequirements

		self.describeBuildResult()

		self.mergePlatformOrBuild(self.platform)

		self.requestedBuildOptions += self.platform._always_build
		self.requestedBuildOptions += node.build

		for role in roles:
			self.fromRole(role)

	def resolveBuildOptions(self, catalog):
		for name in self.requestedBuildOptions:
			if name in self.platform._applied_build_options:
				continue

			build = self.buildContext.resolveBuild(name, catalog)
			if build is None:
				raise ConfigError(f"Node {self.name} wants to provision {name}, but I don't know how")

			if not build.compatibleWithPlatform(self.platform):
				raise ConfigError(f"Node {self.name} wants to provision {name}, but the option is not compatible with the chosen platform")

			self.mergePlatformOrBuild(build)
			self.buildResult.updateFeatures(build.features)
			self.buildResult.resources += build.resources
			self.buildResult._applied_build_options.add(build.name)

	def mergePlatformOrBuild(self, p):
		if p.base_platforms is not None:
			for base in p.base_platforms:
				self.mergePlatformOrBuild(base)
		if isinstance(p, Build) and p.base_builds is not None:
			for base in p.base_builds:
				self.mergePlatformOrBuild(base)

		self.features.update(p.features)
		self.resources += p.resources
		self.install += p.install
		self.start += p.start
		self.requires += p.requires

		for stage in p.stages.values():
			if stage.only == 'build' and self.name != 'build':
				debug("Skipping stage %s from %s (marked as %s only)" % (stage.name, p, stage.only))
				continue

			if stage.only == 'once':
				if stage.name in self.platform._applied_stages:
					debug(f"Skipping stage {stage.name} from {p} (marked as 'once' and already applied)")
					continue
				self.buildResult._applied_stages.append(stage.name)

			self.mergeStage(stage)

		self.backends.merge(p.backends)

		self._shellActions.update(p.shellActions)

		# Loop over all specified repos. If a repo is marked with
		# "enabled = True", we enable it right away.
		for repo in p.repositories.values():
			if repo.enabled and repo not in self.repositories:
				self.repositories.append(repo)

	def describeBuildResult(self):
		base = self.platform

		result = Platform(base.name)
		result.vendor = base.vendor
		result.os = base.os
		result._base_platforms.insert(0, base.name)
		result._applied_build_options.update(base._applied_build_options)
		result.updateFeatures(self.features)
		result.built_from = base.name

		self.buildResult = result
		return result

##################################################################
# A catalog represents all config files of a certain kind
# (eg all platform files, all resource files, etc).
##################################################################
class Catalog:
	# directoryName is a relative name, like platform.d or resource.d
	# infoClass is a subclass of ConfigFile
	def __init__(self, locations, directoryName, fileContentClass):
		self._locations = locations
		self._directoryName = directoryName
		self._fileContentClass = fileContentClass

	def files(self, dirs = None):
		if dirs is None:
			dirs = self._locations.all_config_dirs

		for basedir in dirs:
			path = os.path.join(basedir, self._directoryName)
			if os.path.isdir(path):
				for de in os.scandir(path):
					if not de.is_file() or not de.name.endswith(".conf"):
						continue

					yield self._fileContentClass(de.path)

	# Given a config file name (foo.conf) try to locate the
	# file in a number of places.
	# Note that user directories (added by Config.addDirectory()) take
	# precedence over the standard ones like /etc/twopence.
	def locateConfig(self, filename, directoryName = None):
		if directoryName is None:
			directoryName = self._directoryName

		if not filename.endswith(".conf"):
			filename += ".conf"

		debug(f"Looking for {filename}")
		for basedir in self._locations.all_config_dirs:
			path = os.path.join(basedir, directoryName, filename)
			if os.path.exists(path):
				debug(f"  found {path}")
				return self._fileContentClass(path)
		debug(f"  no cigar")
		return None

class ConfigFile(Configurable):
	def __init__(self, path, loadFile = True):
		super().__init__()
		self.path = path
		if loadFile:
			self.configureFromPath(path)

	def __str__(self):
		return self.path
		return f"{self.__class__.__name__}({self.path})"

##################################################################
# Platform catalog
##################################################################
class PlatformCatalog(Catalog):
	def __init__(self, locations):
		super().__init__(locations, "platform.d", PlatformConfigFile)

	def platformByName(self, name):
		for file in self.files():
			platform = file.getPlatform(name)
			if platform is not None:
				return platform
		return None

	# Given a platform name, return the file that contains it
	def locatePlatformFileByName(self, name):
		for file in self.files():
			platform = file.getPlatform(name)
			if platform is not None:
				return file
		return None

	def locatePlatformsForOS(self, requestedOS, backend, architecture, dirs = None):
		# debug(f"locatePlatformsForOS(os={requestedOS}, backend={backend}, architecture={architecture})")
		for file in self.files():
			for platform in file.platforms:
				if platform.os == requestedOS and \
				   platform.hasImageFor(backend, architecture):
					yield platform

	@property
	def platforms(self):
		for fi in self.files():
			for platform in fi.platforms:
				yield platform

	def getPlatform(self, name):
		for fi in self.files():
			platform = fi.getPlatform(name)
			if platform is not None:
				return platform
		return None

	@property
	def builds(self):
		for fi in self.files():
			for build in fi.builds:
				yield build

	def getBuild(self, name):
		for fi in self.files():
			build = fi.getPlatform(name)
			if build is not None:
				return build
		return None

class PlatformConfigFile(ConfigFile):
	schema = [
		DictNodeSchema('_platforms', 'platform', itemClass = Platform),
		DictNodeSchema('_builds', 'build', itemClass = Build),
		DictNodeSchema('_requirements', 'requirement', itemClass = ConfigRequirement),
	]

	@property
	def builds(self):
		return self._builds.values()

	@property
	def platforms(self):
		return self._platforms.values()

	@property
	def requirements(self):
		return self._requirements.values()

	def getPlatform(self, name):
		return self._platforms.get(name)

	def getBuild(self, name):
		return self._builds.get(name)

##################################################################
# Build catalog
#
# This catalog works slightly different from the platform catalog.
# When a build refers to a "base build" named foo, we do not search
# all files in build.d/ for a build named foo. Instead, we want
# to open build.d/foo.conf and add all builds defined there, no
# matter what they're called.
##################################################################
class BuildCatalog(Catalog):
	def __init__(self, locations):
		super().__init__(locations, "build.d", BuildConfigFile)

	def locateFileByName(self, name):
		return self.locateConfig(name)

class BuildConfigFile(ConfigFile):
	schema = [
		DictNodeSchema('_builds', 'build', itemClass = Build),
	]

	@property
	def builds(self):
		return self._builds.values()

	def getBuild(self, name):
		return self._builds.get(name)

##################################################################
# Requirement catalog
#
# This catalog works slightly different from the platform catalog.
# When a build refers to a "base build" named foo, we do not search
# all files in build.d/ for a build named foo. Instead, we want
# to open build.d/foo.conf and add all builds defined there, no
# matter what they're called.
##################################################################
class RequirementCatalog(Catalog):
	def __init__(self, locations):
		super().__init__(locations, '', RequirementConfigFile)

	def locateFileByName(self, name):
		return self.locateConfig(name)

	def saveResponse(self, name, response):
		config_dir = self._locations.default_user_config_dir
		if config_dir is None:
			warning(f"Cannot save data for requirement {name} - no user config dir set")
			return None

		path = os.path.join(config_dir, f"{name}.conf")
		file = self._fileContentClass(path, loadFile = False)
		file._info[name] = response
		file.publishToPath(path)

class RequirementConfigFile(ConfigFile):
	schema = [
		DictNodeSchema('_info', 'info', itemClass = ConfigOpaque),
	]

	@property
	def responses(self):
		return self._info.values()

	def getResponse(self, name):
		return self._info.get(name)

##################################################################
# Application catalog
##################################################################
class ApplicationCatalog(Catalog):
	def __init__(self, locations):
		super().__init__(locations, "application.d", ApplicationInfo)

	def locateApplicationsForOS(self, applicationID, requestedOS, backend, architecture, dirs = None):
		for file in self.files():
			for application in file.applications:
				if application.id == applicationID and \
				   (requestedOS is None or application.os == requestedOS) and \
				   application.hasImageFor(backend, architecture):
					yield application

class ApplicationInfo(ConfigFile):
	schema = [
		DictNodeSchema('_applications', 'application', itemClass = Application),
	]

	@property
	def applications(self):
		return self._applications.values()

	def getApplication(self, name):
		return self._applications.get(name)

##################################################################
# BuildContext - this is what ties all the info from various
# config sources together
##################################################################
class BuildContext:
	def __init__(self, file, platform):
		self.path = file.path		# for list-platforms
		self.platform = platform
		self.satisfiedRequirements = None

		self._platforms = {}
		self._builds = {}
		self._requirements = {}

		self._mergeFile(file)

	def __str__(self):
		return self.platform.name

	def _mergeFile(self, file):
		self._mergeDict(self._platforms, file, 'platforms')
		self._mergeDict(self._platforms, file, 'applications')
		self._mergeDict(self._builds, file, 'builds')
		self._mergeDict(self._requirements, file, 'requirements')

	def _mergeDict(self, myDict, file, attr_name):
		objectIter = getattr(file, attr_name, None)
		if objectIter is None:
			debug(f"{file} has no {attr_name}")
			return

		for object in objectIter:
			if object.name not in myDict:
				myDict[object.name] = object

	@property
	def platforms(self):
		return self._platforms.values()

	def getPlatform(self, name):
		return self._platforms.get(name)

	@property
	def builds(self):
		return self._builds.values()

	def getBuild(self, name):
		return self._builds.get(name)

	def getRequirement(self, name):
		return self._requirements.get(name)

	def validate(self):
		platform = self.platform
		if not platform.vendor or not platform.os:
			raise ConfigError(f"Node {node.name} uses platform {platform.name}, which lacks a vendor and os definition")

	def resolveBasePlatforms(self, platform, catalog):
		if platform.base_platforms is not None:
			return

		debug(f"resolving base platforms of platform {platform.name}")
		platform.base_platforms = []
		for name in platform._base_platforms:
			base = self.getPlatform(name)
			if base is None:
				file = catalog.locatePlatformFileByName(name)
				if file is None:
					raise ConfigError(f"Cannot find base platform \"{name}\" of platform \"{platform.name}\"")

				base = file.getPlatform(name)
				self._mergeFile(file)

			platform.base_platforms.append(base)

		for base in platform.base_platforms:
			self.resolveBasePlatforms(base, catalog)
			platform.updateFeatures(base.features)

		# debug(f"platform {platform.name} has features {platform.features}")
		return platform.base_platforms

	def resolveBaseBuilds(self, build, catalog):
		if build.base_builds is not None:
			return build.base_builds

		debug(f"resolving base builds of build {build.name}")
		build.base_builds = []
		for name in build._base_builds:
			base = self.loadBuild(name, catalog)
			if base is None:
				raise ConfigError(f"Cannot find base build \"{name}\" of build \"{build.name}\"")

			build.base_builds.append(base)

		for base in build.base_builds:
			self.resolveBaseBuilds(base, catalog)

		return build.base_builds

	def resolveImage(self, backendName):
		return self.platform.resolveImage(backendName)

	def resolveRequirements(self, requirementsManager, nodeName):
		satisfied = []

		platform = self.platform
		if platform.requires:
			for name in platform.requires:
				# For now, we only consider requirements included in the platform file.
				req = self.getRequirement(name)
				if req is None:
					raise ConfigError(f"node {nodeName} requires \"{name}\" but I can't find a description for it")

				response = None
				if requirementsManager:
					response = requirementsManager.handle(nodeName, req)

				if response is None:
					raise ConfigError(f"node {nodeName} requires \"{name}\" but I don't know how to provide it")

				satisfied.append(response)

		self.satisfiedRequirements = satisfied

	def resolveBuild(self, name, catalog):
		build = self.loadBuild(name, catalog)
		if build is not None:
			self.resolveBaseBuilds(build, catalog)
		return build

	def loadBuild(self, name, catalog):
		found = self.getBuild(name)
		if found is None:
			file = catalog.locateFileByName(name)
			if file is None:
				return None

			found = file.getBuild(name)
			self._mergeFile(file)
		return found

class Config(Configurable):
	schema = [
		IgnoredAttributeSchema('default-port'),
		IgnoredNodeSchema('defaults'),
		StringAttributeSchema('workspaceRoot', 'workspace-root'),
		StringAttributeSchema('workspace'),
		StringAttributeSchema('backend'),
		StringAttributeSchema('testcase'),
		DictNodeSchema('_applications', 'application', itemClass = Application),
		DictNodeSchema('_backends', 'backend', itemClass = ConfigOpaque),
		DictNodeSchema('_roles', 'role', itemClass = Role),
		DictNodeSchema('_nodes', 'node', itemClass = Node),
		ListAttributeSchema('_repositories', 'repository'),
		ParameterNodeSchema('_parameters', 'parameters'),

		DictNodeSchema('_compatibility', 'compatibility', itemClass = ConfigOpaque),
	]

	def __init__(self, workspace):
		super().__init__()

		self.workspace = workspace
		self.logspace = None

		self.status = None
		self._requirementsManager = None
		self._locations = Locations()

		self.platformCatalog = PlatformCatalog(self._locations)
		self.buildCatalog = BuildCatalog(self._locations)
		self.requirementsCatalog = RequirementCatalog(self._locations)
		self.applicationCatalog = ApplicationCatalog(self._locations)

		self.defaultRole = self._roles.create("default")

		self._valid = False

	def addDirectory(self, path):
		self._locations.addDirectory(path)

	# Given a config file name (foo.conf) try to locate the 
	# file in a number of directories.
	# Note that user directories (added by .addDirectory() above) take
	# precedence over the standard ones like /etc/twopence.
	def locateConfig(self, filename):
		for basedir in self._locations.all_config_dirs:
			path = os.path.join(basedir, filename)
			if os.path.exists(path):
				return path
		return None

	def locatePlatformFiles(self):
		return iter(self.platformCatalog.files())

	def locatePlatformsForOS(self, requestedOS, backend, architecture):
		return iter(self.platformCatalog.locatePlatformsForOS(requestedOS, backend, architecture))

	def locateApplicationsForOS(self, application, requestedOS, backend, architecture):
		return iter(self.applicationCatalog.locateApplicationsForOS(application, requestedOS, backend, architecture))

	def locateBuildTargets(self):
		for file in self.locatePlatformFiles():
			for platform in file.platforms:
				if platform.os:
					yield self.createBuildContext(file, platform)

	def load(self, filename):
		filename = self.locateConfig(filename)
		if filename is None:
			return False

		self.configureFromPath(filename)
		return True

	def validate(self, purpose = None):
		if purpose == "testing":
			if not self.testcase:
				raise ConfigError("no testcase name configured")
			if not self.nodes:
				raise ConfigError("no nodes configured")

		if self._valid:
			return

		if not self.workspace:
			raise ConfigError("no workspace configured")

		self._valid = True

	@property
	def platforms(self):
		return self.platformCatalog.platforms

	def buildContextForPlatform(self, name):
		for file in self.platformCatalog.files():
			platform = file.getPlatform(name)
			if platform is not None:
				return self.createBuildContext(file, platform)

		return None

	def buildContextForApplication(self, name):
		for file in self.applicationCatalog.files():
			application = file.getApplication(name)
			if application is not None:
				return self.createBuildContext(file, application)

		return None

	def createBuildContext(self, file, platform):
		context = BuildContext(file, platform)
		context.resolveBasePlatforms(platform, self.platformCatalog)

		for build in list(context.builds):
			context.resolveBaseBuilds(build, self.buildCatalog)

		for build in context.builds:
			debug(f"platform {platform.name} supports build option {build.name}")

		return context

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
	def parameters(self):
		return self._parameters

	@parameters.setter
	def parameters(self, value):
		assert(isinstance(value, dict))
		self._parameters.update(value)

	@property
	def requirementsManager(self):
		return self._requirementsManager

	@requirementsManager.setter
	def requirementsManager(self, value):
		assert(isinstance(value, RequirementsManager))
		self._requirementsManager = value

	def configureBackend(self, backend):
		backendConfig = self._backends.get(backend.name)
		if backendConfig is not None:
			# backendConfig is a ConfigOpaque instance, while Configurable.configue
			# expects a curly.Config object. However, ConfigOpaque implements
			# enough of the curly behavior to make this call work.
			backend.configure(backendConfig)

	def findBuildNode(self):
		result = None
		for node in self.nodes:
			if node.build:
				if result:
					raise ConfigError("More than one node with a build target; unable to handle")
				result = node

		return result

	def finalizeNode(self, node, backend):
		roles = self.rolesForNode(node)

		buildContext = self.platformForNode(backend, node, roles)

		result = FinalNodeConfig(node, buildContext, roles)

		# FIXME: fold all of the rest into buildContext as well

		# Extract and apply backend specific configuration from platform and node
		backendNode = backend.attachNode(result)

		# Now resolve all requested build options.
		# We do it here, because the backend may request additional build options
		# in backend.attachNode() above
		result.resolveBuildOptions(self.buildCatalog)

		# And finally, configure the backend specific options for this node,
		# such as template, url, etc but also the timeout
		result.configureBackend(backend.name, backendNode)
		debug(f"Backend {backend.name} configured {node.name} as {backendNode}")

		for repo in result.repositories:
			if not buildContext.platform.repositoryIsActive(repo):
				result.activate_repositories.append(repo)

		return result

	@staticmethod
	def createEmptyNode(name, workspace = None):
		return EmptyNodeConfig(name)

	def createNode(self, name):
		return self._nodes.create(name)

	def rolesForNode(self, node):
		roles = []
		for name in (node.role, "default"):
			role = self.getRole(name)
			if role:
				roles.append(role)
		return roles

	def platformForNode(self, backend, node, roles):
		buildContext = self._platformForNode(node, roles)

		buildContext.validate()

		if not buildContext.resolveImage(backend.name):
			raise ConfigError(f"Unable to determine {backend.name} image for platform {buildContext}")

		buildContext.resolveRequirements(self._requirementsManager, node.name)

		return buildContext

	def _platformForNode(self, node, roles):
		def tryPlatform(name, originType, originName):
			platform = self.buildContextForPlatform(name)
			if platform:
				return platform
			raise ConfigError(f"Cannot find platform \"{name}\" for {originType} {originName}")

		def tryApplication(name, originType, originName):
			application = self.buildContextForApplication(name)
			if application:
				return application
			raise ConfigError(f"Cannot find application \"{name}\" for {originType} {originName}")

		if node.platform:
			return tryPlatform(node.platform, "node", node.name)
		if node.application:
			return tryApplication(node.application, "node", node.name)

		for role in roles:
			if role.platform:
				return tryPlatform(role.platform, "role", role.name)
			if role.application:
				return tryApplication(role.application, "role", role.name)

		raise ConfigError("No platform defined for node \"%s\" (role \"%s\")" % (node.name, node.role))

##################################################################
# Handle requirements
# Front-end should derive from this
##################################################################
class RequirementsManager(object):
	def __init__(self, catalog):
		self.catalog = catalog
		self._cache = dict()
		self._configs = []

	# This should be implemented by subclasses
	# It should return a dict mapping item name to value
	def prompt(self, nodeName, req):
		return None

	def handle(self, nodeName, req):
		# First, let's see if we cached it during a previous call
		response = req.getCachedResponse(nodeName)

		# If not, see if can load it from the requirements catalog
		if response is None:
			response = req.loadResponse(nodeName, self.catalog)

		# If that failed, too, prompt the user for it
		if response is None:
			data = self.prompt(nodeName, req)
			if data:
				response = req.buildResponse(nodeName, data)

			if req.shouldSaveResponse(response):
				self.catalog.saveResponse(req.name, response)

		return response

##################################################################
# debug helper
##################################################################
def dumpBackendDict(who, backends):
	if backends:
		print(f"{who} defines backends")
		for b in backends.values():
			print(f"  {b.name}")
			b.config.save("/dev/stdout")

##################################################################
# This must happen at the very end of the file:
##################################################################
Schema.initializeAll(globals())
