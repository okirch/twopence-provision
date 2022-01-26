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

from .instance import *
from .logging import *
from .paths import *
from .provision import ProvisioningScriptCollection, ProvisioningShellEnvironment

class ConfigError(Exception):
	pass

##################################################################
# Some of the stuff here is rather generic, ie not directly related
# to twopence-provision, and should probably go elsewhere...
##################################################################

##################################################################
# Type conversions
##################################################################
class TypeConversion(object):
	@staticmethod
	def from_string(value):
		raise NotImplementedError()

	@staticmethod
	def to_string(value):
		return str(value)

class TypeConversionInt(TypeConversion):
	type_name = "int"

	@staticmethod
	def from_string(value):
		return int(value)

class TypeConversionFloat(TypeConversion):
	type_name = "float"

	@staticmethod
	def from_string(value):
		return float(value)

class TypeConversionBool(TypeConversion):
	type_name = "bool"

	@staticmethod
	def from_string(value):
		if value is None:
			return False
		value = value.lower()
		if value in ('true', 'yes', 'on', '1'):
			return True
		if value in ('false', 'no', 'off', '0'):
			return False
		raise ValueError("Unable to convert \"%s\" to boolean" % value)


##################################################################
# Define correspondence between attributes in a curly config file,
# and a python object's members
##################################################################
class Schema(object):
	debug_init_enabled = False
	debug_enabled = False

	def __init__(self, name, key):
		self.name = name
		self.key = key or name

	@classmethod
	def debug_init(klass, msg):
		if klass.debug_init_enabled:
			print(f"DEBUG: {msg}")

	@classmethod
	def debug(klass, msg):
		if klass.debug_enabled:
			print(f"DEBUG: {msg}")

	@staticmethod
	def initializeAll(ctx):
		class_type = type(object)

		for thing in ctx.values():
			if type(thing) is not class_type:
				continue

			if issubclass(thing, Configurable):
				schema = getattr(thing, "schema", None)
				if schema:
					Schema.initializeClass(thing)

				Schema.initializeAll(thing.__dict__)
			elif issubclass(thing, AttributeSchema) and thing != AttributeSchema or \
			     issubclass(thing, NodeSchema) and thing != NodeSchema:
				# export class FooAttributeSchema as Schema.FooAttribute
				Schema.publishSchemaClass(thing)

	@staticmethod
	def initializeClass(klass):
		assert(klass.schema is not None)

		klass._attributes = {}
		klass._nodes = {}

		Schema.debug_init("%s: initialize schema" % (klass.__name__,))
		for item in klass.schema:
			Schema.debug_init(f"   {item}")
			if isinstance(item, AttributeSchema):
				klass._attributes[item.key] = item
			elif isinstance(item, NodeSchema):
				klass._nodes[item.key] = item
			else:
				raise TypeError(item.__class__.__name__)

	def publishSchemaClass(klass):
		name = klass.__name__
		assert(name.endswith('Schema'))
		name = name[:-6]
		Schema.debug_init("Publish %s as Schema.%s" % (klass.__name__, name))
		setattr(Schema, name, klass)

class AttributeSchema(Schema):
	def __init__(self, name, key = None, typeconv = None):
		super().__init__(name, key)
		self.typeconv = typeconv

	def initialize(self, obj):
		setattr(obj, self.name, copy.copy(self.default_value))

	def __str__(self):
		info = [self.name]
		if self.key != self.name:
			info.append("key='%s'" % self.key)
		if self.typeconv:
			info.append("type=%s" % self.typeconv.type_name)
		return "%s(%s)" % (self.__class__.__name__, ", ".join(info))

	# same function for scalar and list
	def publish(self, obj, config):
		value = getattr(obj, self.name, None)
		if value not in (None, [], ""):
			typeconv = self.typeconv
			if typeconv:
				if type(value) == list:
					value = [typeconv.to_string(_) for _ in value]
				else:
					value = typeconv.to_string(value)
			Schema.debug("   %s = %s" % (self.key, value))
			config.set_value(self.key, value)

class ScalarAttributeSchema(AttributeSchema):
	def update(self, obj, attr):
		value = attr.value
		if value is not None:
			if self.typeconv:
				value = self.typeconv.from_string(value)
			setattr(obj, self.name, value)

class StringAttributeSchema(ScalarAttributeSchema):
	default_value = None

class BooleanAttributeSchema(ScalarAttributeSchema):
	default_value = False

	def __init__(self, name, key = None):
		super().__init__(name, key, typeconv = TypeConversionBool)

class IntegerAttributeSchema(ScalarAttributeSchema):
	default_value = 0

	def __init__(self, name, key = None):
		super().__init__(name, key, typeconv = TypeConversionInt)

class FloatAttributeSchema(ScalarAttributeSchema):
	def __init__(self, name, key = None, default_value = 0.0):
		super().__init__(name, key, typeconv = TypeConversionInt)
		self.default_value = default_value

class ListAttributeSchema(AttributeSchema):
	default_value = []

	def update(self, obj, attr):
		# attr.values may return None or []
		values = attr.values
		if values:
			if self.typeconv:
				values = [self.typeconv.from_string(_) for _ in values]

			current = getattr(obj, self.name)
			assert(type(current) == list)
			setattr(obj, self.name, current + values)

##################################################################
# Define correspondence between nodes in a curly file, and
# objects in python
##################################################################
class NodeSchema(Schema):
	def __init__(self, name, key, containerClass):
		super().__init__(name, key)
		self.containerClass = containerClass

	def initialize(self, obj):
		setattr(obj, self.name, self.containerClass())

	def __str__(self):
		info = [self.name]
		if self.key != self.name:
			info.append("key='%s'" % self.key)
		info.append("container=%s" % self.containerClass)
		return "%s(%s)" % (self.__class__.__name__, ", ".join(info))

	def getContainerFor(self, obj):
		containerObject = getattr(obj, self.name, None)
		if containerObject is None:
			raise ValueError("Node %s has no member %s" % (obj, self.name))
		return containerObject

	def update(self, obj, node):
		self.debug("Updating %s object's %s by creating %s(%s)" % (
			obj.__class__.__name__, self.name,
			node.type, node.name))

		container = self.getContainerFor(obj)
		item = container.create(node.name)
		item.configure(node)

		if True:
			Schema.debug("Defined %s" % item)

	def publish(self, obj, node):
		self.debug("Publishing %s object's %s" % (obj.__class__.__name__, self.name))

		container = self.getContainerFor(obj)
		container.publish(node)

class DictNodeSchema(NodeSchema):
	def __init__(self, name, key = None, containerClass = None, itemClass = None):
		if containerClass is None:
			if not itemClass:
				raise ValueError("DictNodeSchema must specifiy either container or item class")

			containerClass = lambda: ConfigDict(key or name, itemClass)

		super().__init__(name, key, containerClass)

class ListNodeSchema(NodeSchema):
	def __init__(self, name, key = None, itemClass = None):
		containerClass = lambda: ConfigList(key or name, itemClass)
		super().__init__(name, key, containerClass)

	def create(self, name):
		return ConfigRequirement.Item(name)

class ParameterNodeSchema(NodeSchema):
	def __init__(self, name, key = None):
		super().__init__(name, key, containerClass = dict)

	def update(self, obj, node):
		container = self.getContainerFor(obj)
		for attr in node.attributes:
			container[attr.name] = attr.value

	def publish(self, obj, node):
		container = self.getContainerFor(obj)

		child = node.add_child(self.key)
		for key, value in container.items():
			child.set_value(key, value)

##################################################################
# Used to ignore nodes or attrs in a config file
##################################################################
class IgnoredAttributeSchema(AttributeSchema):
	def __init__(self, key):
		super().__init__(key, key)

	def initialize(self, obj):
		pass

	def update(self, obj, config):
		pass

	def publish(self, obj, config):
		pass

class IgnoredNodeSchema(NodeSchema):
	def __init__(self, key):
		super().__init__(key, key, None)

	def initialize(self, obj):
		pass

	def update(self, obj, config):
		pass

	def publish(self, obj, config):
		pass

##################################################################
# This is a helper class that simplifies how we populate a
# python object from a curly config file.
##################################################################
class Configurable(object):
	info_attrs = []

	schema = None
	_attributes = None
	_nodes = None

	def __init__(self):
		if self.schema:
			for info in self._attributes.values():
				info.initialize(self)

			for info in self._nodes.values():
				info.initialize(self)

	def configureFromPath(self, path):
		debug("Loading %s" % path)
		config = curly.Config(path)
		self.configure(config.tree())

	def configure(self, config):
		assert(self.schema)
		if not config:
			return

		Schema.debug(f"Configuring {self}")
		for attr in config.attributes:
			Schema.debug("   %s = %s" % (attr.name, attr.values))
			info = self.__class__._attributes.get(attr.name)
			if info is None:
				raise KeyError("Unknown configuration key %s in node %s" % (attr.name, config))
			info.update(self, attr)

		for child in config:
			Schema.debug("   %s %s { ... }" % (child.type, child.name))
			info = self.__class__._nodes.get(child.type)
			if info is None:
				raise KeyError("Unknown configuration key %s in node %s" % (child.type, config))
			info.update(self, child)

	def publish(self, config):
		assert(self.schema)

		Schema.debug(f"Publishing {self}")
		# Write out all bits of information in the order defined by the schema
		for info in self.schema:
			Schema.debug(f"{info}.publish({self})")
			info.publish(self, config)
		return

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

# common case: a Configurable with a name, represented by
#	type name {
#		bla; blah; blah;
#	}
class NamedConfigurable(Configurable):
	def __init__(self, name):
		super().__init__()
		self.name = name

class ConfigList(list):
	def __init__(self, type_name, item_class, verbose = False):
		self.type_name = type_name
		self.item_class = item_class
		self.verbose = verbose

	def __str__(self):
		return "[%s]" % " ".join([str(_) for _ in self])

	def create(self, name):
		item = self.item_class(name)
		self.append(item)
		return item

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
class ConfigRequirement(NamedConfigurable):
	info_attrs = ['name', 'provides', 'valid']

	class Item(NamedConfigurable):
		schema = [
			StringAttributeSchema('prompt'),
			StringAttributeSchema('default'),
		]

	class Fnord(NamedConfigurable):
		info_attrs = ['name']

		def __init__(self, name, data = None):
			super().__init__(name)
			self.data = data or {}

		def configure(self, config):
			for name in config.get_attributes():
				self.data[name] = config.get_value(name)

		def publish(self, curlyNode):
			for key, value in self.data.items():
				curlyNode.set_value(key, value)

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

	def publish(self, config):
		for saved in self.values():
			grand_child = config.add_child("backend", saved.name)
			for config in saved.configs:
				for attr_name in config.get_attributes():
					values = config.get_values(attr_name)
					if not values:
						values = [""]
					grand_child.set_value(attr_name, values)

class Repository(NamedConfigurable):
	info_attrs = ['name', 'url']

	schema = [
		StringAttributeSchema('url'),
		StringAttributeSchema('keyfile'),
		BooleanAttributeSchema('enabled'),
		BooleanAttributeSchema('active'),
	]

class Imageset(NamedConfigurable):
	info_attrs = ['name']

	class Architecture(NamedConfigurable):
		schema = [
			DictNodeSchema('backends', 'backend', containerClass = BackendDict),
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
		for path in self.paths():
			if not os.path.isfile(path):
				raise ConfigError("Script snippet \"%s\" does not exist" % path)

		self.invocations = []
		for cmd in self.perform:
			if cmd:
				self.addInvocation(cmd)

	def addInvocation(self, cmd):
		debug(f"Action {self.name}: add invocation {cmd}")
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

	def load(self, alreadyLoaded = None):
		result = []
		for path in self.paths():
			if path in alreadyLoaded:
				continue

			debug("Trying to load script snippet from %s" % path)

			result += ["", "# BEGIN %s" % path]
			with open(path, "r") as f:
				result += f.read().split('\n')
				result.append("# END OF %s" % path)

			alreadyLoaded.add(path)

		result += self.commands

		for invocation in self.invocations:
			result += ["",
				f"# Expanded from {invocation.name}",
				f"twopence_exec {invocation.command}"]

		return result

	def paths(self):
		result = []

		stagedir = os.path.join("/usr/lib/twopence/provision", self.category)
		for name in self.run:
			path = os.path.join(stagedir, name)
			result.append(path)

		stagedir = "/usr/lib/twopence/provision"
		for invocation in self.invocations:
			if invocation.path is not None:
				debug(f"path for {invocation} is {invocation.path}")
				path = os.path.join(stagedir, invocation.path)
				result.append(path)

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

class Platform(NamedConfigurable):
	info_attrs = ['name', 'image', 'vendor', 'os', 'imagesets', 'requires',
			'repositories',
			'features', 'install', 'start', 'resources']

	schema = [
		StringAttributeSchema('vendor'),
		StringAttributeSchema('os'),
		StringAttributeSchema('arch'),
		StringAttributeSchema('image'),		# obsolete?
		ListAttributeSchema('features'),
		ListAttributeSchema('resources'),
		ListAttributeSchema('requires'),
		ListAttributeSchema('_base_platforms', 'use-base-platforms'),
		StringAttributeSchema('keyfile', 'ssh-keyfile'),
		StringAttributeSchema('build_time', 'build-time'),
		ListAttributeSchema('install'),
		ListAttributeSchema('start'),

		DictNodeSchema('repositories', 'repository', itemClass = Repository),
		DictNodeSchema('imagesets', 'imageset', itemClass = Imageset),
		DictNodeSchema('stages', 'stage', itemClass = BuildStage),
		DictNodeSchema('backends', 'backend', containerClass = BackendDict),
		DictNodeSchema('shellActions', 'shell', itemClass = ShellAction),
	]

	def __init__(self, name):
		super().__init__(name)

		self.base_platforms = None

		# used during build, exclusively
		self._raw_key = None

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
		result.resources = self.resources
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

	def resolveBasePlatforms(self, config):
		if self.base_platforms is not None:
			return

		self.base_platforms = []
		for name in self._base_platforms:
			base = config.getPlatform(name)
			if base is None:
				raise ConfigError("Cannot find base platform \"%s\" of platform \"%s\"" % (name, self.name))

			self.base_platforms.append(base)

		return self.base_platforms

	def mergeBackendConfigs(self, backendConfigs):
		if not backendConfigs.configs:
			return

		saved = self.backends.create(backendConfigs.name)
		saved.configs = backendConfigs.configs + saved.configs

class Role(NamedConfigurable):
	info_attrs = ["name", "platform", "repositories", "features",]

	schema = [
		StringAttributeSchema('platform'),
		ListAttributeSchema('repositories'),
		ListAttributeSchema('install'),
		ListAttributeSchema('start'),
		ListAttributeSchema('features'),
	]

class Node(NamedConfigurable):
	info_attrs = ["name", "role", "platform", "build"]

	schema = [
		StringAttributeSchema('role'),
		StringAttributeSchema('platform'),
		ListAttributeSchema('build'),
		ListAttributeSchema('install'),
		ListAttributeSchema('start'),
		DictNodeSchema("_backends", "backend", BackendDict),
	]

class Build(Platform):
	info_attrs = Platform.info_attrs + ['base_platform']

	schema = Platform.schema + [
		StringAttributeSchema('base_platform', key = 'base-platform'),
		# obsolete:
		StringAttributeSchema('template'),
	]

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
		self.resources = []
		self.backends = BackendDict()
		self.satisfiedRequirements = None
		self._stages = {}
		self._shellActions = {}

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

			self.repositories.append(repo)

		self.install += role.install
		self.start += role.start
		self.features += role.features

	def persistInfo(self, nodePersist):
		nodePersist.features = self.features
		nodePersist.resources = self.resources
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
		self.buildGenericStage("install-packages", self.install, action = "install-package")
		self.buildGenericStage("start-services", self.start, action = "start-service")
		self.buildGenericStage("add-repositories",
						filter(lambda repo: not repo.active, self.repositories),
						actionFunc = lambda repo:
							f"install-repository {repo.url} {repo.name}"
					)

		for stage in self.stages:
			stage.resolveActions(self._shellActions)

		stage = self.createStage('preamble')
		if stage.is_empty:
			stage.run.append("preamble")

		if verbose_enabled():
			verbose(f"Provisioning recipe for node {self.name}:")
			for stage in self.stages:
				verbose(f" Stage {stage.name} reboot={stage.reboot}")
				for name in stage.run:
					verbose(f"    run {stage.category}/{name}")
				for invocation in stage.invocations:
					verbose(f"    {invocation.command}")
					if invocation.path:
						verbose(f"      (script sourced from {invocation.path})")

		return ProvisioningScriptCollection(self.stages, self.exportShellVariables())

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
		if p.base_platforms is not None:
			for base in p.base_platforms:
				self.mergePlatformOrBuild(base)

		self.features += p.features
		self.resources += p.resources
		self.install += p.install
		self.start += p.start
		self.requires += p.requires

		for stage in p.stages.values():
			if stage.only == 'build' and self.name != 'build':
				print("Skipping stage %s (marked as %s only)" % (stage.name, stage.only))
				continue

			self.mergeStage(stage)

		self._shellActions.update(p.shellActions)

		# Loop over all specified repos. If a repo is marked with
		# "enabled = True", we enable it right away.
		for repo in p.repositories.values():
			if repo.enabled and repo not in self.repositories:
				self.repositories.append(repo)

			self.buildResult.repositories.add(repo)

		self.buildResult.features += p.features
		self.buildResult.resources += p.resources

		# This is a pretty blunt axe: the platform we're building inherits
		# all actions from the base platform and the build options.
		self.buildResult.shellActions.update(p.shellActions)

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

	schema = [
		IgnoredAttributeSchema('default-port'),
		IgnoredNodeSchema('defaults'),
		StringAttributeSchema('workspaceRoot', 'workspace-root'),
		StringAttributeSchema('workspace'),
		StringAttributeSchema('testcase'),
		DictNodeSchema('_backends', 'backend', containerClass = BackendDict),
		DictNodeSchema('_platforms', 'platform', itemClass = Platform),
		DictNodeSchema('_roles', 'role', itemClass = Role),
		DictNodeSchema('_nodes', 'node', itemClass = Node),
		DictNodeSchema('_builds', 'build', itemClass = Build),
		DictNodeSchema('_requirements', 'requirement', itemClass = ConfigRequirement),
		ListAttributeSchema('_repositories', 'repository'),
		ParameterNodeSchema('_parameters', 'parameters'),
	]

	def __init__(self, workspace):
		super().__init__()

		self.workspace = workspace
		self.logspace = None

		self.status = None
		self._requirementsManager = None
		self._user_config_dirs = []

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

		schema = [
			DictNodeSchema('_platforms', 'platform', itemClass = Platform),
			DictNodeSchema('_builds', 'build', itemClass = Build),
			StringAttributeSchema('build_time', 'build-time'),
		]

		def __init__(self, path):
			self.path = path
			self.configureFromPath(path)

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

		self.configureFromPath(filename)
		return True

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
		if found:
			found.resolveBasePlatforms(self)
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

##################################################################
# This must happen at the very end of the file:
##################################################################
Schema.initializeAll(globals())
