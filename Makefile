include Make.defs

LIBDIR	= /usr/lib

TWP_ETCDIR	= $(TWOPENCE_ETCDIR)
TWP_BINDIR	= $(TWOPENCE_BINDIR)
TWP_LIBDIR	= $(PROVISION_LIBDIR)
TWP_PYDIR	= $(PYTHON_INSTDIR)/twopence/provision

CONFIGS		= provision.conf \
		  suse-registration.conf

all: ;

install::
	mkdir -p $(DESTDIR)$(TWP_BINDIR)
	@install -vm 555 twopence-provision $(DESTDIR)$(TWP_BINDIR)/provision
	mkdir -p $(DESTDIR)$(TWP_ETCDIR)
	@install -vm 644 etc/*.conf $(DESTDIR)$(TWP_ETCDIR)
	@cp -av etc/platform.d $(DESTDIR)$(TWP_ETCDIR)
	@cp -av etc/resource.d $(DESTDIR)$(TWP_ETCDIR)
	@cp -av etc/build.d $(DESTDIR)$(TWP_ETCDIR)
	@cp -av etc/application.d $(DESTDIR)$(TWP_ETCDIR)
	mkdir -p $(DESTDIR)$(TWP_LIBDIR)
	@install -vm 444 templates/Vagrantfile.in $(DESTDIR)$(TWP_LIBDIR)
	@for script_dir in shell; do \
		src_dir=templates/$$script_dir; \
		[ -d "$$src_dir" ] || continue; \
		dest_dir="$(DESTDIR)$(TWP_LIBDIR)/$$script_dir"; \
		mkdir -p $$dest_dir; \
		cp -v $$src_dir/* $$dest_dir; \
	done
	mkdir -p $(DESTDIR)$(TWP_PYDIR)
	cp -vr python/_twopence/*.py $(DESTDIR)$(TWP_PYDIR)
