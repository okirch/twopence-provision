##################################################################
#
# module __init__ file for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from .logging import *
from .manifest import BOM
from .backend import Backend
from .topology import TestTopology
from .config import Config, ConfigError, RequirementsManager

def __buildDummyConfig():
	import twopence

	config = Config("/no/where")

	for path in twopence.global_config_files:
		config.load(path)

	# Note: we load global config files first; THEN
	# we add user directories to the config search path.
	config.addDirectory(twopence.user_config_dir)
	return config

def queryPlatformFeatures(platformName):
	config = __buildDummyConfig()

	platform = config.getPlatform(platformName)
	if platform is None:
		return None

	return platform.features

def locatePlatformFiles():
	config = __buildDummyConfig()

	for pi in config.locatePlatformFiles():
		yield pi

def locatePlatformsForOS(os, backend, architecture = None):
	config = __buildDummyConfig()

	for platform in config.locatePlatformsForOS(os, backend, architecture):
		yield platform

def locateBasePlatformForOS(os, backend, architecture = None):
	config = __buildDummyConfig()

	return config.locateBasePlatformForOS(os, backend, architecture)
