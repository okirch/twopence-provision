##################################################################
#
# Generic support classes for container based backends
#
# Copyright (C) 2022 Olaf Kirch <okir@suse.de>
#
##################################################################
import os
import json

from .logging import *
from .backend import Backend
from .runner import Runner
from .network import *
from .config import *

from .oci import ImageFormatDockerRegistry, ImageReference, ImageConfig, ContainerStatus

# container's security settings
class ContainerSecurityConfig(Configurable):
	info_attrs = ['privileged', 'capabilities']

	schema = [
		BooleanAttributeSchema('privileged'),
		SetAttributeSchema('capabilities'),
	]

# container's startup settings
class ContainerStartupConfig(Configurable):
	schema = [
		StringAttributeSchema('command'),
		ListAttributeSchema('arguments'),
		StringAttributeSchema('success'),
	]

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

# This class provides information on how to configure the container's runtime
class ContainerRuntimeConfig(Configurable):
	schema = [
		SingleNodeSchema('security', itemClass = ContainerSecurityConfig),
		SingleNodeSchema('startup', itemClass = ContainerStartupConfig),
		SingleNodeSchema('volumes', itemClass = ConfigOpaque),
		SingleNodeSchema('sysctl', itemClass = ConfigOpaque),
	]

	@property
	def tmpfs(self):
		return self._tmpfs.values()

class ContainerNodeConfig(Configurable):
	info_attrs = ['registry', 'image', 'timeout']

	schema = [
		Schema.StringAttribute('image'),
		Schema.StringAttribute('registry'),
		Schema.FloatAttribute('timeout', default_value = 120),

		SingleNodeSchema('runtime', 'runtime', itemClass = ContainerRuntimeConfig),
	]

	def __init__(self):
		super().__init__()
		# currently, we do not support specifying a concrete version
		# in the config file. We just record this information when
		# deciding on the exact image to use, so that it can be
		# saved if needed
		self.version = None

	@property
	def key(self):
		return ImageReference(self.registry, self.image)

