#
# Spec for testing some quirks of spec parsing
#

Name:           pkg_name
Summary:        Spec for testing some quirks of spec parsing
Version:        0.1
Release:        1.2
License:        GPLv2
Source1:        foobar.tar.gz

%description
Spec for testing some quirks of spec parsing. No intended for building an RPM.

%prep
# We don't have Source0 so rpmbuild would fail, but gbp shouldn't crash
%setup -q

