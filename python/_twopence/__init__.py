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

def queryPlatformFeatures(platformName):
	import twopence

	config = Config("/no/where")

	for path in twopence.global_config_files:
		config.load(path)

	# Note: we load global config files first; THEN
	# we add user directories to the config search path.
	config.addDirectory(twopence.user_config_dir)

	platform = config.getPlatform(platformName)
	if platform is None:
		return None

	return set(platform.features)
