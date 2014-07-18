# vim: set fileencoding=utf-8 :
#
# (C) 2013 Intel Corporation <markus.lehtonen@linux.intel.com>
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""Unit tests for the gbp-buildpackage-rpm tool"""

import glob
import mock
import os
import re
import shutil
import stat
import subprocess
from nose.tools import assert_raises, eq_, ok_ # pylint: disable=E0611

from gbp.git import GitRepository
from gbp.scripts.buildpackage_rpm import main as gbp_rpm

from tests.component.rpm import RpmRepoTestBase, RPM_TEST_DATA_DIR
from tests.testutils import ls_dir, ls_tar, ls_zip

# Disable "Method could be a function warning"
#   pylint: disable=R0201
# Disable "Too many public methods"
#   pylint: disable=R0904


DATA_DIR = os.path.join(RPM_TEST_DATA_DIR, 'rpm')
ORIG_DATA_DIR = os.path.join(RPM_TEST_DATA_DIR, 'orig')

MOCK_NOTIFICATIONS = []


def mock_gbp(args):
    """Wrapper for gbp-buildpackage-rpm"""
    return gbp_rpm(['arg0', '--git-notify=off'] + args +
                   ['-ba', '--clean', '--target=noarch', '--nodeps'])

def mock_notify(summary, message, notify_opt):
    """Mock notification system"""
    # Auto will succeed
    if notify_opt.is_auto():
        MOCK_NOTIFICATIONS.append((summary, message))
        return True
    # Otherwise fail
    return False


class TestGbpRpm(RpmRepoTestBase):
    """Basic tests for git-rpm-ch"""

    @staticmethod
    def ls_rpm(rpm):
        """List the contents of an rpm package"""
        args = ['rpm', '-q', '--qf',
                '[%{FILEDIGESTS %{FILEMODES} %{FILENAMES}\n]', '-p']
        popen = subprocess.Popen(args + [rpm], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        stdout, stderr = popen.communicate()
        if popen.returncode:
            raise Exception("Failed to get file metadata for %s: %s" %
                            (rpm, stderr))
        return sorted([(nam, mod, dig) for dig, mod, nam in
                        [lin.split(None, 2) for lin in stdout.splitlines()]])

    @staticmethod
    def check_rpms(directory):
        """Check build results"""
        # Only check files, at least for now
        files = glob.glob(directory + '/*rpm')
        assert files, "No rpms (%s)found in %s" % (files, directory)
        for path in files:
            ref_file = os.path.join(DATA_DIR, os.path.basename(path))
            eq_(TestGbpRpm.ls_rpm(path), TestGbpRpm.ls_rpm(ref_file))

    @staticmethod
    def check_and_rm_file(filepath, content):
        """Check file content and remove it"""
        with open(filepath) as fobj:
            eq_(fobj.read(), content)
        os.unlink(filepath)

    @classmethod
    def setup_class(cls, **kwargs):
        """Setup unit tests"""
        # Don't mangle branch names so that we're able to build the packages
        super(TestGbpRpm, cls).setup_class(mangle_branch_names=False, **kwargs)

    def test_invalid_args(self):
        """Check graceful exit when called with invalid args"""
        GitRepository.create('.')
        with assert_raises(SystemExit):
            mock_gbp(['--git-invalid-arg'])

    def test_outside_repo(self):
        """Run outside a git repository"""
        eq_(mock_gbp([]), 1)
        self._check_log(0, 'gbp:error: %s is not a git repository' %
                            os.path.abspath('.'))

    def test_invalid_config_file(self):
        """Test invalid config file"""
        # Create and commit dummy invalid config file
        repo = GitRepository.create('.')
        with open('.gbp.conf', 'w') as conffd:
            conffd.write('foobar\n')
        repo.add_files('.gbp.conf')
        repo.commit_all('Add conf')
        eq_(mock_gbp([]), 1)
        self._check_log(0, 'gbp:error: File contains no section headers.')

    def test_native_build(self):
        """Basic test of native pkg"""
        self.init_test_repo('gbp-test-native')
        eq_(mock_gbp([]), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        shutil.rmtree('../rpmbuild')

        eq_(mock_gbp(['--git-native=off']), 2)
        self._check_log(0, 'gbp:error: Invalid upstream treeish upstream/')

    def test_native_build2(self):
        """Basic test of another native pkg"""
        self.init_test_repo('gbp-test-native2')
        eq_(mock_gbp([]), 0)
        self.check_rpms('../rpmbuild/RPMS/*')

    def test_non_native_build(self):
        """Basic test of non-native pkg"""
        self.init_test_repo('gbp-test')
        eq_(mock_gbp([]), 0)
        self.check_rpms('../rpmbuild/RPMS/*')

    def test_option_native(self):
        """Test the --git-native option"""
        self.init_test_repo('gbp-test2')
        eq_(mock_gbp([]), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        shutil.rmtree('../rpmbuild')

        # Building this pkg should succeed, but no patches generated,
        # only one "manually maintained" patch
        eq_(mock_gbp(['--git-native=on']), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        eq_(len(glob.glob('../rpmbuild/SOURCES/*patch')), 1)

    def test_options_ignore(self):
        """Test the --git-ignore-[new|untracked] options"""
        self.init_test_repo('gbp-test-native')

        # Create an untracked file
        with open('untracked-file', 'w') as fobj:
            fobj.write('this file is not tracked\n')

        eq_(mock_gbp([]), 1)
        eq_(mock_gbp(['--git-ignore-untracked']), 0)
        self.check_rpms('../rpmbuild/RPMS/*')

        # Modify tracked file
        with open('README', 'a') as fobj:
            fobj.write('new stuff\n')

        eq_(mock_gbp(['--git-ignore-untracked']), 1)
        eq_(mock_gbp(['--git-ignore-new']), 0)

    @mock.patch('gbp.notifications.notify', mock_notify)
    def test_option_notify(self):
        """Test the --git-notify option"""
        self.init_test_repo('gbp-test-native')

        eq_(mock_gbp(['--git-notify=auto']), 0)
        summary, message = MOCK_NOTIFICATIONS.pop()
        ok_(re.match(r'Gbp-rpm successful', summary), summary)
        ok_(re.match(r'Build of \S+ \S+ succeeded', message), message)

        # Mock-notification will fail with "on" setting
        eq_(mock_gbp(['--git-notify=on']), 1)
        self._check_log(-1, "gbp:error: Failed to send notification")

        # No notification when "off"
        eq_(mock_gbp(['--git-notify=off']), 0)
        eq_(len(MOCK_NOTIFICATIONS), 0)

    def test_option_tmp_dir(self):
        """Test the --git-tmp-dir option"""
        self.init_test_repo('gbp-test-native')

        eq_(mock_gbp(['--git-tmp-dir=../gbptmp', '--git-no-build']), 0)
        ok_(os.path.isdir('../gbptmp'))

        # Check tmpdir access/creation error
        os.chmod('../gbptmp', 0)
        try:
            eq_(mock_gbp(['--git-tmp-dir=../gbptmp/foo', '--git-no-build']), 1)
        finally:
            os.chmod('../gbptmp', stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)

    def test_tagging(self):
        """Test tagging options"""
        repo = self.init_test_repo('gbp-test-native')

        # Build and tag
        eq_(mock_gbp(['--git-tag', '--git-packaging-tag=rel-tag']), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        ok_(repo.has_tag('rel-tag'))
        sha = repo.rev_parse('HEAD')
        eq_(sha, repo.rev_parse('rel-tag^0'))
        self.check_rpms('../rpmbuild/RPMS/*')

        # Should fail if the tag already exists
        eq_(mock_gbp(['--git-tag', '--git-packaging-tag=rel-tag']), 1)

        # Re-tag
        eq_(mock_gbp(['--git-retag', '--git-packaging-tag=rel-tag']), 1)
        self._check_log(-1, "gbp:error: '--git-retag' needs either '--git-tag'")

        eq_(mock_gbp(['--git-tag', '--git-packaging-tag=rel-tag',
                     '--git-retag', '--git-export=HEAD^']), 0)
        ok_(repo.has_tag('rel-tag'))
        sha2 = repo.rev_parse('HEAD^')
        ok_(sha2 != sha)
        eq_(sha2, repo.rev_parse('rel-tag^0'))

        # Tag-only
        shutil.rmtree('../rpmbuild')
        eq_(mock_gbp(['--git-tag-only', '--git-packaging-tag=rel-tag2']), 0)
        ok_(not os.path.exists('../rpmbuild'))
        ok_(repo.has_tag('rel-tag2'))

        # Valid tag format string keys
        tag_keys = ['upstreamversion', 'release', 'version', 'vendor',
                    'nowtime', 'authortime', 'committime',
                    'nowtimenum', 'authortimenum', 'committimenum']
        # Should fail if the fag format has invalid keys (foo here)
        tag_fmt = '_'.join(['%(' + key + ')s' for key in tag_keys + ['foo']])
        eq_(mock_gbp(['--git-tag', '--git-packaging-tag=%(foo)s']), 1)
        # Remove 'foo' and should succeed
        tag_fmt = '_'.join(['%(' + key + ')s' for key in tag_keys])
        eq_(mock_gbp(['--git-tag-only', '--git-packaging-tag=%s' % tag_fmt]), 0)
        # New tag with same format should succeed when '*num' keys are present
        eq_(mock_gbp(['--git-tag-only', '--git-packaging-tag=%s' % tag_fmt]), 0)

    def test_option_upstream_tree(self):
        """Test the --git-upstream-tree option"""
        repo = self.init_test_repo('gbp-test')

        # Dummy update to upstream branch
        pkg_branch = repo.get_branch()
        upstr_branch = 'srcdata/gbp-test/upstream'
        orig_files = ['gbp-test/' + path for \
                path in self.ls_tree(repo, upstr_branch)] + ['gbp-test']
        repo.set_branch(upstr_branch)
        with open('new-file', 'w') as fobj:
            fobj.write('New file\n')
        with open('new-file2', 'w') as fobj:
            fobj.write('New file 2\n')
        repo.add_files(['new-file', 'new-file2'])
        repo.commit_files('new-file', 'New content')
        repo.commit_files('new-file2', 'New content 2')
        repo.set_branch(pkg_branch)

        # TAG (default) does not contain the new files
        eq_(mock_gbp([]), 0)
        tar_files = ls_tar('../rpmbuild/SOURCES/gbp-test-1.1.tar.bz2')
        self.check_files(orig_files, tar_files)
        shutil.rmtree('../rpmbuild')

        # Branch contains them both
        eq_(mock_gbp(['--git-upstream-tree=BRANCH']), 0)
        tar_files = ls_tar('../rpmbuild/SOURCES/gbp-test-1.1.tar.bz2')
        self.check_files(orig_files +
                         ['gbp-test/new-file', 'gbp-test/new-file2'], tar_files)
        shutil.rmtree('../rpmbuild')

        # The first "extra-commit" in upstream contains only one new file
        eq_(mock_gbp(['--git-upstream-tree=%s^' % upstr_branch]), 0)
        tar_files = ls_tar('../rpmbuild/SOURCES/gbp-test-1.1.tar.bz2')
        self.check_files(orig_files + ['gbp-test/new-file'], tar_files)
        shutil.rmtree('../rpmbuild')

        # Test invalid upstream treeish
        eq_(mock_gbp(['--git-upstream-tree=TAG',
                      '--git-upstream-tag=invalid-tag']), 2)
        self._check_log(-1, ".*Invalid upstream treeish invalid-tag")
        eq_(mock_gbp(['--git-upstream-tree=BRANCH', '--git-native=no',
                      '--git-upstream-branch=invalid-branch']), 2)
        self._check_log(-1, ".*invalid-branch is not a valid branch")
        eq_(mock_gbp(['--git-upstream-tree=invalid-tree']), 2)
        self._check_log(-1, ".*Invalid treeish object")

    def test_option_orig_prefix(self):
        """Test the --git-orig-prefix option"""
        repo = self.init_test_repo('gbp-test')

        # Building with invalid prefix should fail
        eq_(mock_gbp(['--git-orig-prefix=foo']), 1)
        upstr_branch = 'srcdata/gbp-test/upstream'
        ref_files = ['foo/' + path for path in self.ls_tree(repo, upstr_branch)]
        tar_files = ls_tar('../rpmbuild/SOURCES/gbp-test-1.1.tar.bz2', False)
        self.check_files(tar_files, ref_files)

        # Test invalid keys
        eq_(mock_gbp(['--git-orig-prefix=%(foo)s', '--git-no-build']), 1)
        self._check_log(-1, ".*Unknown key 'foo' in orig prefix format")

    def test_pristine_tar(self):
        """Test pristine-tar"""
        repo = self.init_test_repo('gbp-test')

        # Pristine-tar checkout fails when no pristine-tar branch
        eq_(mock_gbp(['--git-pristine-tar',
                      '--git-export=srcdata/gbp-test/release/1.1-2']), 1)
        self._check_log(-1, ".*Couldn't checkout")

        # Create pristine-tar branch and try again
        repo.create_branch('pristine-tar', 'srcdata/gbp-test/pristine_tar')
        eq_(mock_gbp(['--git-pristine-tar',
                      '--git-export=srcdata/gbp-test/release/1.1-2']), 0)
        self.check_rpms('../rpmbuild/RPMS/*')

    def test_pristine_tar_commit(self):
        """Test committing upstream tarball to pristine-tar"""
        repo = self.init_test_repo('gbp-test')

        eq_(repo.has_branch('pristine-tar'), False)
        eq_(mock_gbp(['--git-pristine-tar-commit',
                      '--git-export=srcdata/gbp-test/release/1.0-1']), 0)
        eq_(len(repo.get_commits(until='pristine-tar')), 1)
        shutil.rmtree('../rpmbuild')

        # Using --git-pristine-tar and --git-pristine-tar-commit should be ok
        eq_(mock_gbp(['--git-pristine-tar', '--git-pristine-tar-commit']), 0)
        eq_(len(repo.get_commits(until='pristine-tar')), 2)
        shutil.rmtree('../rpmbuild')

        # Second time no pristine-tar should not be commited
        eq_(mock_gbp(['--git-pristine-tar-commit']), 0)
        eq_(len(repo.get_commits(until='pristine-tar')), 2)

    def test_tarball_dir(self):
        """Test a separate tarball cache"""
        self.init_test_repo('gbp-test')

        # Create and populate tarball cache
        tarball_dir = '../tarballs'
        os.mkdir(tarball_dir)
        shutil.copy2(os.path.join(ORIG_DATA_DIR, 'gbp-test-1.0.tar.bz2'),
                     tarball_dir)

        # Test build when tarball is found from cache
        eq_(mock_gbp(['--git-export=srcdata/gbp-test/release/1.0-1',
                      '--git-tarball-dir=%s' % tarball_dir]), 0)
        ok_(os.path.islink(os.path.join('..', 'rpmbuild', 'SOURCES',
                                        'gbp-test-1.0.tar.bz2')))

        # Test build when tarball is not found from cache
        eq_(mock_gbp(['--git-export=srcdata/gbp-test/release/1.1-2',
                      '--git-tarball-dir=%s' % tarball_dir]), 0)
        ok_(os.path.isfile(os.path.join('..', 'rpmbuild', 'SOURCES',
                                        'gbp-test-1.1.tar.bz2')))

    def test_packaging_branch_options(self):
        """Test the --packaging-branch and --ignore-branch cmdline options"""
        repo = self.init_test_repo('gbp-test-native')

        eq_(mock_gbp(['--git-packaging-branch=foo']), 1)
        self._check_log(-2, "gbp:error: You are not on branch 'foo'")

        eq_(mock_gbp(['--git-packaging-branch=foo', '--git-ignore-branch']), 0)

        # Test building when not on any branch
        repo.set_branch(repo.rev_parse('HEAD'))
        eq_(mock_gbp(['--git-no-build']), 1)
        eq_(mock_gbp(['--git-ignore-branch', '--git-no-build']), 0)

    def test_option_submodules(self):
        """Test the --git-submodules option"""
        repo = self.init_test_repo('gbp-test')

        # Create submodule to upstream branch
        sub_repo = self.orig_repos['gbp-test-native']
        pkg_branch = repo.get_branch()
        upstr_branch = 'srcdata/gbp-test/upstream'
        repo.set_branch(upstr_branch)
        repo.add_submodule(sub_repo.path)
        repo.commit_all('Add submodule')
        repo.set_branch(pkg_branch)

        sub_files = self.ls_tree(sub_repo, 'HEAD')
        upstr_files = ['gbp-test/' + path for
                            path in self.ls_tree(repo, upstr_branch)]

        # Test the "no" option
        eq_(mock_gbp(['--git-no-submodules', '--git-upstream-tree=%s' %
                      upstr_branch, '--git-ignore-untracked']), 0)
        tar_files = ls_tar('../rpmbuild/SOURCES/gbp-test-1.1.tar.bz2', False)
        self.check_files(upstr_files, tar_files)
        shutil.rmtree('../rpmbuild')

        # Test the "yes" option
        eq_(mock_gbp(['--git-submodules', '--git-upstream-tree=%s' %
                      upstr_branch, '--git-ignore-untracked']), 0)
        tar_files = ls_tar('../rpmbuild/SOURCES/gbp-test-1.1.tar.bz2', False)
        ref_files = upstr_files + ['gbp-test/gbp-test-native.repo/' + path for
                                        path in sub_files]
        self.check_files(ref_files, tar_files)
        shutil.rmtree('../rpmbuild')

        # Test submodule failure
        shutil.rmtree('gbp-test-native.repo')
        repo.create('gbp-test-native.repo')
        eq_(mock_gbp(['--git-submodules', '--git-upstream-tree=%s' %
                      upstr_branch, '--git-ignore-untracked']), 2)

    def test_option_submodules_native(self):
        """Test the --git-submodules option for native packages"""
        repo = self.init_test_repo('gbp-test-native')

        # Create submodule
        sub_repo = self.orig_repos['gbp-test-native2']
        repo.add_submodule(sub_repo.path)
        repo.commit_all('Add submodule')

        sub_files = self.ls_tree(sub_repo, 'HEAD')
        master_files = ['gbp-test-native-1.0/' + path for
                            path in self.ls_tree(repo, 'HEAD')]

        # Test
        eq_(mock_gbp(['--git-submodules']), 0)
        zip_files = ls_zip('../rpmbuild/SOURCES/gbp-test-native-1.0.zip', False)
        ref_files = master_files + \
                    ['gbp-test-native-1.0/gbp-test-native2.repo/' + path for
                                        path in sub_files]
        self.check_files(ref_files, zip_files)

        # Test submodule failure
        shutil.rmtree('gbp-test-native2.repo')
        repo.create('gbp-test-native2.repo')
        eq_(mock_gbp(['--git-submodules', '--git-ignore-untracked']), 1)

    def test_option_builder(self):
        """Test --git-builder option and it's args"""
        self.init_test_repo('gbp-test-native')
        base_args = ['arg0', '--git-notify=off']

        # Try rpmbuild with default args
        eq_(gbp_rpm(base_args + ['--git-builder=rpmbuild', '--nodeps']), 0)

        # Build without builder args
        builder_script = 'echo -n $* > builder_args.txt'
        eq_(gbp_rpm(base_args + ['--git-builder=%s' % builder_script]), 0)
        with open('../rpmbuild/builder_args.txt') as fobj:
            args = fobj.read()
        eq_(args, 'gbp-test-native.spec')

        # Build with builder args
        eq_(gbp_rpm(base_args + ['--git-builder=%s' % builder_script,
                                 '--arg1', '--arg2']), 0)
        with open('../rpmbuild/builder_args.txt') as fobj:
            args = fobj.read()
        eq_(args, '--arg1 --arg2 gbp-test-native.spec')

    def test_option_builder_osc(self):
        """Test --git-builder=osc"""
        self.init_test_repo('gbp-test-native')
        eq_(mock_gbp(['--git-builder=osc', '--git-no-build']), 0)

        eq_(set(os.listdir('../rpmbuild')),
            set(os.listdir('./packaging') + ['gbp-test-native-1.0.zip']))

    def test_option_cleaner(self):
        """Test --git-cleaner option"""
        self.init_test_repo('gbp-test-native')

        # Make repo dirty
        with open('untracked-file', 'w') as fobj:
            fobj.write('this file is not tracked\n')

        # Build on dirty repo should fail
        eq_(mock_gbp([]), 1)

        # Build should succeed with cleaner
        eq_(mock_gbp(['--git-cleaner=rm untracked-file']), 0)

    def test_hook_options(self):
        """Test different hook options"""
        self.init_test_repo('gbp-test-native')

        cleaner = 'echo -n cleaner >> ../hooks'
        postexport = 'echo -n postexport >> $GBP_TMP_DIR/../hooks'
        prebuild = 'echo -n prebuild >> $GBP_BUILD_DIR/../hooks'
        postbuild = 'echo -n postbuild >> $GBP_BUILD_DIR/../hooks'
        posttag = 'echo -n posttag >> ../hooks'
        args = ['--git-cleaner=%s' % cleaner,
                '--git-postexport=%s' % postexport,
                '--git-prebuild=%s' % prebuild,
                '--git-postbuild=%s' % postbuild,
                '--git-posttag=%s' % posttag]

        # Only cleaner and posttag is run when tagging
        eq_(mock_gbp(args + ['--git-tag-only', '--git-packaging-tag=tag1']), 0)
        self.check_and_rm_file('../hooks', 'cleanerposttag')

        # Prebuild is not run when only exporting
        eq_(mock_gbp(args + ['--git-no-build']), 0)
        self.check_and_rm_file('../hooks', 'cleanerpostexport')
        shutil.rmtree('../rpmbuild')

        # Export and build scripts are run when not tagging
        eq_(mock_gbp(args), 0)
        self.check_and_rm_file('../hooks', 'cleanerpostexportprebuildpostbuild')
        shutil.rmtree('../rpmbuild')

        # All hooks are run when building
        eq_(mock_gbp(args + ['--git-tag', '--git-packaging-tag=tag2']), 0)
        self.check_and_rm_file('../hooks',
                               'cleanerpostexportprebuildpostbuildposttag')
        shutil.rmtree('../rpmbuild')

        # Run with hooks disabled
        eq_(mock_gbp(args + ['--git-no-hooks']), 0)
        ok_(not os.path.exists('../hooks'))

    def test_option_export_only(self):
        """Test the (deprecated) --git-export-only option"""
        self.init_test_repo('gbp-test-native')
        eq_(mock_gbp(['--git-export-only']), 0)
        self._check_log(-1, ".*Deprecated option '--git-export-only'")

    def test_builddir_options(self):
        """Test the options related to different build directories"""
        self.init_test_repo('gbp-test-native')

        eq_(mock_gbp(['--git-export-dir=../foo',
                      '--git-rpmbuild-builddir=build',
                      '--git-rpmbuild-buildrootdir=buildroot',
                      '--git-rpmbuild-rpmdir=rpm',
                      '--git-rpmbuild-sourcedir=source',
                      '--git-rpmbuild-specdir=spec',
                      '--git-rpmbuild-srpmdir=srpm']), 0)

        # Check all directories
        eq_(set(os.listdir('../foo')),
            set(['build', 'buildroot', 'rpm', 'source', 'spec', 'srpm']))

        # Test export dir creation error (gbp will not create dir hierarchy)
        eq_(mock_gbp(['--git-export-dir=../bar/foo']), 1)
        self._check_log(-1, ".*gbp:error: Cannot create dir")

    def test_export_failure(self):
        """Test export dir permission problems"""
        self.init_test_repo('gbp-test-native')
        s_rwx = stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC

        # Pre-create all files
        eq_(mock_gbp(['--git-no-build']), 0)

        # Error in exporting packaging files
        os.chmod('../rpmbuild/SOURCES', 0)
        try:
            eq_(mock_gbp(['--git-no-build']), 1)
        finally:
            os.chmod('../rpmbuild/SOURCES', s_rwx)
        self._check_log(-1, ".*Error exporting files")

        # Error in creating archive
        os.chmod('../rpmbuild/SOURCES/gbp-test-native-1.0.zip', 0)
        try:
            eq_(mock_gbp(['--git-no-build']), 1)
        finally:
            os.chmod('../rpmbuild/SOURCES/gbp-test-native-1.0.zip', s_rwx)
        self._check_log(-1, ".*Error creating ../rpmbuild/SOURCES/.*.zip")

    def test_option_export(self):
        """Test the --git-export-option"""
        repo = self.init_test_repo('gbp-test')

        # Test exporting of some other commit than HEAD
        eq_(mock_gbp(['--git-export=srcdata/gbp-test/release/1.0-1']), 0)
        eq_(os.listdir('../rpmbuild/RPMS/noarch'),
                       ['gbp-test-1.0-1.noarch.rpm'])
        self.check_rpms('../rpmbuild/RPMS/*')

        # Modify one tracked file, create one untracked and one ignored file
        with open('foo.txt', 'a') as fobj:
            fobj.write('staged')
            fobj.flush()
            repo.add_files('foo.txt')
            fobj.write('unstaged')
        with open('untracked', 'w') as fobj:
            fobj.write('untracked')
        with open('ignored.tmp', 'w') as fobj:
            fobj.write('ignored')

        base_args = ['--git-ignore-new', '--git-no-build']
        # Test exporting of git index
        foo_txt_index = repo.show('HEAD:foo.txt') + 'staged'
        eq_(mock_gbp(base_args + ['--git-export=INDEX']), 0)
        self.check_and_rm_file('../rpmbuild/SOURCES/foo.txt', foo_txt_index)
        ok_(not os.path.exists('../rpmbuild/SOURCES/untracked'))
        ok_(not os.path.exists('../rpmbuild/SOURCES/ignored.tmp'))
        shutil.rmtree('../rpmbuild')

        # Test exporting of working copy (tracked files only)
        eq_(mock_gbp(base_args + ['--git-export=WC.TRACKED']), 0)
        foo_txt_wc = repo.show('HEAD:foo.txt') + 'staged' + 'unstaged'
        self.check_and_rm_file('../rpmbuild/SOURCES/foo.txt', foo_txt_wc)
        ok_(not os.path.exists('../rpmbuild/SOURCES/untracked'))
        ok_(not os.path.exists('../rpmbuild/SOURCES/ignored.tmp'))
        shutil.rmtree('../rpmbuild')

        # Test exporting of working copy (include untracked files)
        eq_(mock_gbp(base_args + ['--git-export=WC.UNTRACKED']), 0)
        self.check_and_rm_file('../rpmbuild/SOURCES/foo.txt', foo_txt_wc)
        self.check_and_rm_file('../rpmbuild/SOURCES/untracked', 'untracked')
        ok_(not os.path.exists('../rpmbuild/SOURCES/ignored.tmp'))
        shutil.rmtree('../rpmbuild')

        # Test exporting of working copy (include all files)
        eq_(mock_gbp(base_args + ['--git-export=WC']), 0)
        self.check_and_rm_file('../rpmbuild/SOURCES/foo.txt', foo_txt_wc)
        self.check_and_rm_file('../rpmbuild/SOURCES/untracked', 'untracked')
        self.check_and_rm_file('../rpmbuild/SOURCES/ignored.tmp', 'ignored')
        shutil.rmtree('../rpmbuild')

        # Test exporting an invalid treeish
        eq_(mock_gbp(base_args + ['--git-export=invalid-treeish']), 1)
        self._check_log(-1, "gbp:error: Failed to determine export treeish")

    def test_option_spec_file(self):
        """Test the --git-spec-file cmdline option"""
        repo = self.init_test_repo('gbp-test2')

        eq_(mock_gbp(['--git-spec-file=foo.spec']), 1)
        self._check_log(-1, "gbp:error: Can't parse spec: Git error")

        eq_(mock_gbp(['--git-spec-file=auto']), 1)
        self._check_log(-1, "gbp:error: Can't parse spec: Multiple spec files")

        eq_(mock_gbp(['--git-spec-file=packaging/gbp-test2.spec']), 0)

        # No spec file found error
        repo.set_branch('srcdata/gbp-test2/upstream')
        eq_(mock_gbp([]), 1)
        self._check_log(-1, ".*Can't parse spec: No spec file found")

    def test_option_packaging_dir(self):
        """Test the --git-packaging-dir cmdline option"""
        self.init_test_repo('gbp-test-native')

        eq_(mock_gbp(['--git-packaging-dir=foo']), 1)
        self._check_log(-1, "gbp:error: Can't parse spec: No spec file found")

        # Packaging dir should be taken from spec file if it is defined
        eq_(mock_gbp(['--git-packaging-dir=foo',
                      '--git-spec-file=packaging/gbp-test-native.spec']), 0)

    def test_option_spec_vcs_tag(self):
        """Test the --git-spec-vcs-tag cmdline option"""
        repo = self.init_test_repo('gbp-test-native')

        eq_(mock_gbp(['--git-spec-vcs-tag=foobar-%(commit)s']), 0)
        sha1 = repo.rev_parse('HEAD')
        num_tags = 0
        with open('../rpmbuild/SPECS/gbp-test-native.spec') as fobj:
            for line in fobj.readlines():
                if line.startswith('VCS: '):
                    ok_(re.match(r'VCS:\s+foobar-%s\n$' % sha1, line))
                    num_tags += 1
        eq_(num_tags, 1)

        # Test invalid key
        eq_(mock_gbp(['--git-spec-vcs-tag=%(invalid-key)s']), 1)
        self._check_log(-1, ".*Unknown key 'invalid-key' in vcs tag format")

    def test_patch_export_options(self):
        """Test patch export options"""
        repo = self.init_test_repo('gbp-test2')

        # Test no-patch-export
        base_args = ['--git-builder=osc', '--git-no-build']
        eq_(mock_gbp(base_args + ['--git-no-patch-export']), 0)
        ref_files = self.ls_tree(repo, 'HEAD:packaging')
        ref_files.add('gbp-test2-2.0.tar.gz')
        eq_(ls_dir('../rpmbuild', False), ref_files)
        shutil.rmtree('../rpmbuild')

        # No patches should be generated if patch-export-rev is upstream version

        # Test patch compression and numbering
        eq_(mock_gbp(base_args + ['--git-no-patch-numbers',
                                  '--git-patch-export-compress=1']), 0)
        new_files = ls_dir('../rpmbuild', False) - ref_files
        ok_(len(new_files) > 0)
        for fname in new_files:
            # Patches should start with an alphabet and be compressed with gz
            ok_(re.match(r'^[a-zA-Z]\S*.patch.gz$', fname), fname)

    def test_devel_branch_support(self):
        """Test patch-generation from q/development branch"""
        repo = self.init_test_repo('gbp-test')
        pq_br = 'srcdata/gbp-test/pq/master'

        # Patch export with no apparent pq branch should fail
        eq_(mock_gbp(['--git-patch-export']), 2)
        self._check_log(-1, r".*Start commit \S+ not an ancestor of end commit")

        # With valid pq branch patch export should succeeded
        eq_(mock_gbp(['--git-patch-export', '--git-pq-branch=%s' % pq_br]), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        shutil.rmtree('../rpmbuild')
        eq_(mock_gbp(['--git-patch-export', '--git-pq-branch=%s' % pq_br,
                      '--git-export=srcdata/gbp-test/master']), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        shutil.rmtree('../rpmbuild')

        # With pq branch but with wrong patch-export rev build should fail
        eq_(mock_gbp(['--git-patch-export', '--git-pq-branch=%s' % pq_br,
                      '--git-patch-export-rev=HEAD']), 2)
        self._check_log(-1, r".*Start commit \S+ not an ancestor of end commit")

        # Patch-export should be auto-enabled when on pq branch
        repo.set_branch(pq_br)
        eq_(mock_gbp(['--git-pq-branch=%s' % pq_br, '--git-ignore-branch']), 0)
        self.check_rpms('../rpmbuild/RPMS/*')
        shutil.rmtree('../rpmbuild')

        # Fail when (apparently) on pq branch but no packaging branch found
        eq_(mock_gbp(['--git-pq-branch=%s' % pq_br, '--git-ignore-branch',
                      '--git-packaging-branch=foo']), 1)
