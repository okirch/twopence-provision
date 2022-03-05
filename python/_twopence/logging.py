##################################################################
#
# Minimal logging stuff
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from twopence import logger, debug, debug_extra, verbose, info, warn, error

def setVerbosity(num):
	global opt_verbose

	opt_verbose = num

def debug_enabled():
	return debug.enabled

def verbose_enabled():
	return verbose.enabled

warning = warn
