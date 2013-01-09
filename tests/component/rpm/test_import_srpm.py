# vim: set fileencoding=utf-8 :
#
# (C) 2012 Intel Corporation <markus.lehtonen@linux.intel.com>
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
"""Basic tests for the git-import-srpm tool"""

import os
import shutil
import urllib2
from nose.plugins.skip import SkipTest
from nose.tools import assert_raises    # pylint: disable=E0611
from mock import Mock

from gbp.scripts.import_srpm import main as import_srpm
from gbp.git import GitRepository
from gbp.rpm import SrcRpmFile

from tests.component import ComponentTestBase
from tests.component.rpm import RPM_TEST_DATA_DIR as DATA_DIR

# Disable "Method could be a function warning"
# pylint: disable=R0201

class TestImportPacked(ComponentTestBase):
    """Test importing of src.rpm files"""

    def test_invalid_args(self):
        """See that import-srpm fails gracefully if called with invalid args"""
        assert import_srpm(['arg0']) == 1
        with assert_raises(SystemExit):
            import_srpm(['arg0', '--invalid-arg=123'])

    def test_basic_import(self):
        """Test importing of non-native src.rpm"""
        srpm = os.path.join(DATA_DIR, 'gbp-test-1.0-1.src.rpm')
        assert import_srpm(['arg0', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test')
        self._check_repo_state(repo, 'master', ['master', 'upstream'])
        # Four commits: upstream, packaging files, one patch and the removal
        # of imported patches
        assert len(repo.get_commits()) == 4

    def test_basic_import2(self):
        """Import package with multiple spec files and full url patch"""
        srpm = os.path.join(DATA_DIR, 'gbp-test2-2.0-0.src.rpm')
        assert import_srpm(['arg0', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test2')
        self._check_repo_state(repo, 'master', ['master', 'upstream'])
        # Four commits: upstream, packaging files, one patch and the removal
        # of imported patches
        assert len(repo.get_commits()) == 4

    def test_basic_import_orphan(self):
        """
        Test importing of non-native src.rpm to separate packaging and
        development branches
        """
        srpm = os.path.join(DATA_DIR, 'gbp-test2-2.0-0.src.rpm')
        assert import_srpm(['arg0', '--orphan-packaging', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test2')
        self._check_repo_state(repo, 'master', ['master', 'upstream'])
        # Only one commit: the packaging files
        assert len(repo.get_commits()) == 1

    def test_basic_native_import(self):
        """Test importing of native src.rpm"""
        srpm = os.path.join(DATA_DIR, 'gbp-test2-2.0-0.src.rpm')
        assert import_srpm(['arg0', '--native', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test2')
        self._check_repo_state(repo, 'master', ['master'])
        # Only one commit: the imported source tarball
        assert len(repo.get_commits()) == 1

    def test_import_no_orig_src(self):
        """Test importing of (native) srpm without orig tarball"""
        srpm = os.path.join(DATA_DIR, 'gbp-test-native2-2.0-0.src.rpm')
        assert import_srpm(['arg0', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test-native2')
        self._check_repo_state(repo, 'master', ['master'])
        # Only one commit: packaging files
        assert len(repo.get_commits()) == 1

    def test_multiple_versions(self):
        """Test importing of multiple versions"""
        srpms = [ os.path.join(DATA_DIR, 'gbp-test-1.0-1.src.rpm'),
                  os.path.join(DATA_DIR, 'gbp-test-1.0-1.other.src.rpm'),
                  os.path.join(DATA_DIR, 'gbp-test-1.1-1.src.rpm') ]
        assert import_srpm(['arg0', srpms[0]]) == 0
        repo = GitRepository('gbp-test')
        self._check_repo_state(repo, 'master', ['master', 'upstream'])
        assert len(repo.get_commits()) == 4
        # Try to import same version again
        assert import_srpm(['arg0', srpms[1]]) == 0
        assert len(repo.get_commits()) == 4
        assert len(repo.get_commits(until='upstream')) == 1
        assert import_srpm(['arg0', '--allow-same-version', srpms[1]]) == 0
        # Added new versio packaging plus one patch
        assert len(repo.get_commits()) == 7
        assert len(repo.get_commits(until='upstream')) == 1
        # Import new version
        assert import_srpm(['arg0', srpms[2]]) == 0
        assert len(repo.get_commits()) == 11
        assert len(repo.get_commits(until='upstream')) == 2
        # Check number of tags
        assert len(repo.get_tags('upstream/*')) == 2
        assert len(repo.get_tags('vendor/*')) == 3

    def test_import_to_existing(self):
        """Test importing to an existing repo"""
        srpm = os.path.join(DATA_DIR, 'gbp-test-1.0-1.src.rpm')

        # Create new repo
        repo = GitRepository.create('myrepo')
        os.chdir('myrepo')
        shutil.copy2('.git/HEAD', 'foobar')
        repo.add_files('.')
        repo.commit_all('First commit')

        # Test importing to non-clean repo
        shutil.copy2('.git/HEAD', 'foobaz')
        assert import_srpm(['arg0', '--create-missing', srpm]) == 1
        self._check_log(0, 'gbp:error: Repository has uncommitted changes')
        self._clear_log()
        os.unlink('foobaz')

        # The first import should fail because upstream branch is missing
        assert import_srpm(['arg0', srpm]) == 1
        self._check_log(-1, 'Also check the --create-missing-branches')
        assert import_srpm(['arg0', '--create-missing', srpm]) == 0
        self._check_repo_state(repo, 'master', ['master', 'upstream'])
        # Four commits: our initial, upstream, packaging files, one patch,
        # and the removal of imported patches
        assert len(repo.get_commits()) == 5

        # The import should fail because missing packaging-branch
        srpm = os.path.join(DATA_DIR, 'gbp-test-1.1-1.src.rpm')
        assert import_srpm(['arg0', '--packaging-branch=foo', srpm]) == 1
        self._check_log(-1, 'Also check the --create-missing-branches')


    def test_filter(self):
        """Test filter option"""
        srpm = os.path.join(DATA_DIR, 'gbp-test-1.0-1.src.rpm')
        assert import_srpm(['arg0', '--filter=README', '--filter=mydir',
                            srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test')
        files = set(['Makefile', 'dummy.sh', 'bar.tar.gz', 'foo.txt',
                 'gbp-test.spec', 'my.patch', 'mydir/', 'mydir/myfile.txt'])
        self._check_repo_state(repo, 'master', ['master', 'upstream'], files)

    def test_misc_options(self):
        """Test various options of git-import-srpm"""
        srpm = os.path.join(DATA_DIR, 'gbp-test2-2.0-0.src.rpm')

        assert import_srpm([
                    'arg0',
                    '--no-patch-import',
                    '--packaging-branch=pack',
                    '--upstream-branch=orig',
                    '--packaging-dir=packaging',
                    '--packaging-tag=ver_%(upstreamversion)s-rel_%(release)s',
                    '--upstream-tag=orig/%(upstreamversion)s',
                    '--author-is-committer',
                    srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test2')
        self._check_repo_state(repo, 'pack', ['pack', 'orig'])
        assert len(repo.get_commits()) == 2
        # Check packaging dir
        assert len(repo.get_commits(paths='packaging')) == 1
        # Check tags
        tags = repo.get_tags()
        assert set(tags) == set(['orig/2.0', 'ver_2.0-rel_0'])
        # Check git committer/author
        info = repo.get_commit_info('pack')
        assert info['author'].name == 'Markus Lehtonen'
        assert info['author'].email == 'markus.lehtonen@linux.intel.com'
        assert info['author'].name == info['committer'].name
        assert info['author'].email == info['committer'].email


class TestImportUnPacked(ComponentTestBase):
    """Test importing of unpacked source rpms"""

    def setup(self):
        super(TestImportUnPacked, self).setup()
        # Unpack some source rpms
        os.mkdir('multi-unpack')
        for pkg in ['gbp-test-1.0-1.src.rpm', 'gbp-test2-2.0-0.src.rpm']:
            unpack_dir = pkg.replace('.src.rpm', '-unpack')
            os.mkdir(unpack_dir)
            pkg_path = os.path.join(DATA_DIR, pkg)
            SrcRpmFile(pkg_path).unpack(unpack_dir)
            SrcRpmFile(pkg_path).unpack('multi-unpack')

    def test_import_dir(self):
        """Test importing of directories"""
        assert import_srpm(['arg0', 'gbp-test-1.0-1-unpack']) == 0
        # Check repository state
        repo = GitRepository('gbp-test')
        self._check_repo_state(repo, 'master', ['master', 'upstream'])

        # Check that importing dir with multiple spec files fails
        assert import_srpm(['arg0', 'multi-unpack']) == 1
        self._check_log(-1, 'gbp:error: Failed determine spec file: '
                               'Multiple spec files found')

    def test_import_spec(self):
        """Test importing of spec file"""
        specfile = 'gbp-test2-2.0-0-unpack/gbp-test2.spec'
        assert import_srpm(['arg0', specfile]) == 0
        # Check repository state
        assert GitRepository('gbp-test2').is_clean()

    def test_missing_files(self):
        """Test importing of directory with missing packaging files"""
        specfile = 'gbp-test2-2.0-0-unpack/gbp-test2.spec'
        os.unlink('gbp-test2-2.0-0-unpack/my.patch')
        assert import_srpm(['arg0', specfile]) == 1
        self._check_log(-1, "gbp:error: File 'my.patch' listed in spec "
                            "not found")


class TestDownloadImport(ComponentTestBase):
    """Test download functionality"""

    def test_urldownload(self):
        """Test downloading and importing src.rpm from remote url"""
        srpm = 'http://raw.github.com/marquiz/git-buildpackage-rpm-testdata/'\
               'master/gbp-test-1.0-1.src.rpm'
        # Mock to use local files instead of really downloading
        local_fn = os.path.join(DATA_DIR, os.path.basename(srpm))
        urllib2.urlopen = Mock()
        urllib2.urlopen.return_value = open(local_fn, 'r')

        assert import_srpm(['arg0', '--download', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test')
        self._check_repo_state(repo, 'master', ['master', 'upstream'])

    def test_nonexistent_url(self):
        """Test graceful failure when trying download from nonexistent url"""
        srpm = 'http://url.does.not.exist.com/foo.src.rpm'
        # Do not connect to remote, mock failure
        urllib2.urlopen = Mock()
        urllib2.urlopen.side_effect = urllib2.HTTPError(srpm, 404, "Not found",
                                                        None, None)

        assert import_srpm(['arg0', '--download', srpm]) == 1
        self._check_log(-1, "gbp:error: Download failed: HTTP Error 404")
        self._clear_log()

    def test_invalid_url(self):
        """Test graceful failure when trying download from invalid url"""
        srpm = 'foob://url.does.not.exist.com/foo.src.rpm'
        assert import_srpm(['arg0', '--download', srpm]) == 1
        self._check_log(-1, "gbp:error: Download failed: unknown url type:")
        self._clear_log()


class TestPristineTar(ComponentTestBase):
    """Test importing with pristine-tar"""

    @classmethod
    def setup_class(cls):
        if not os.path.exists('/usr/bin/pristine-tar'):
            raise SkipTest('Skipping %s:%s as pristine-tar tool is not '
                           'available' % (__name__, cls.__name__))
        super(TestPristineTar, cls).setup_class()

    def test_basic_import_pristine_tar(self):
        """Test importing of non-native src.rpm, with pristine-tar"""
        srpm = os.path.join(DATA_DIR, 'gbp-test-1.0-1.src.rpm')
        assert import_srpm(['arg0', '--pristine-tar', srpm]) == 0
        # Check repository state
        repo = GitRepository('gbp-test')
        self._check_repo_state(repo, 'master', ['master', 'upstream',
                               'pristine-tar'])
        # Four commits: upstream, packaging files, one patch and the removal
        # of imported patches
        assert len(repo.get_commits()) == 4


class TestBareRepo(ComponentTestBase):
    """Test importing to a bare repository"""

    def test_basic_import_to_bare_repo(self):
        """Test importing of srpm to a bare git repository"""
        srpm = os.path.join(DATA_DIR, 'gbp-test-1.0-1.src.rpm')
        # Create new repo
        repo = GitRepository.create('myrepo', bare=True)
        os.chdir('myrepo')
        assert import_srpm(['arg0', srpm]) == 0
        self._check_repo_state(repo, 'master', ['master', 'upstream'])
        # Patch import to bare repos not supported -> only 2 commits
        assert len(repo.get_commits(until='master')) == 2

# vim:et:ts=4:sw=4:et:sts=4:ai:set list listchars=tab\:»·,trail\:·:
