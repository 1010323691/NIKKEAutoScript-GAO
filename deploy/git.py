import os
import shutil
import subprocess
import urllib.request
from functools import cached_property

from deploy.config import DeployConfig, ExecutionError
from module.logger import logger


class GitManager(DeployConfig):
    @cached_property
    def git(self):
        return self.filepath('GitExecutable')

    @staticmethod
    def remove(file):
        try:
            os.remove(file)
            logger.info(f'Removed file: {file}')
        except FileNotFoundError:
            logger.info(f'File not found: {file}')

    def _git_output(self, args) -> str:
        """
        Run a git command quietly and return stdout, '' on any failure.
        """
        try:
            return subprocess.run(
                [self.git, *args],
                capture_output=True,
                text=True,
                encoding='utf8',
            ).stdout.strip()
        except Exception:
            return ''

    @staticmethod
    def _normalize_remote_url(url) -> str:
        url = str(url or '').strip().replace('\\', '/').rstrip('/').lower()
        return url[:-4] if url.endswith('.git') else url

    def update_source(self) -> str:
        """
        Remote that tracks Repository.  A fork keeps its own repository as
        origin and adds the official one under another name (e.g. upstream);
        rewriting origin there would silently repoint the fork.
        """
        repo = self._normalize_remote_url(self.Repository)
        remotes = {}
        for line in self._git_output(['remote', '-v']).splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remotes.setdefault(parts[0], self._normalize_remote_url(parts[1]))
        if remotes.get('origin') == repo:
            return 'origin'
        for name, url in remotes.items():
            if url == repo:
                return name
        return 'origin'

    def _count_commits(self, revision_range) -> int:
        try:
            return int(self._git_output(['rev-list', '--count', revision_range]) or 0)
        except ValueError:
            return 0

    def use_merge_update(self, source, branch) -> bool:
        """
        Local commits that upstream does not have would be destroyed by
        reset --hard, so such checkouts are updated by merging instead.
        """
        if not os.path.exists('./.git'):
            return False
        return source != 'origin' or self._count_commits(f'{source}/{branch}..HEAD') > 0

    def merge_blocker(self, ref) -> str:
        """
        Dry-run merging ref into HEAD without touching the work tree.

        Returns:
            str: '' if the merge is clean, otherwise why it must not be merged.
        """
        try:
            result = subprocess.run(
                [self.git, 'merge-tree', '--write-tree', '--name-only', '--no-messages', 'HEAD', ref],
                capture_output=True,
                text=True,
                encoding='utf8',
            )
        except OSError as e:
            return f'Unable to test merge with {ref}: {e}'
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode == 1:
            return (
                f'Update from {ref} conflicts with local commits, nothing was merged. '
                f'Conflicting files: {self._format_files(lines[1:])}'
            )
        if result.returncode != 0 or not lines:
            return f'Unable to test merge with {ref}: {result.stderr.strip() or result.returncode}'
        incoming = set(self._git_output(['diff', '--name-only', 'HEAD', lines[0]]).splitlines())
        modified = set(self._git_output(['diff', '--name-only', 'HEAD']).splitlines())
        overwritten = sorted(incoming & modified)
        if overwritten:
            return (
                f'Update from {ref} would overwrite uncommitted local changes, nothing was merged. '
                f'Commit or discard them first: {self._format_files(overwritten)}'
            )
        return ''

    @staticmethod
    def _format_files(files, limit=10) -> str:
        files = list(files)
        text = ', '.join(files[:limit])
        if len(files) > limit:
            text += f' (+{len(files) - limit} more)'
        return text or '(unknown)'

    def git_merge_update(self, source, branch):
        logger.hr('Merge Repository Update', 1)
        ref = f'{source}/{branch}'
        # No --depth here: shallowing a working repository would cut off its own history.
        self.execute(f'"{self.git}" fetch {source} {branch}')
        blocker = self.merge_blocker(ref)
        if blocker:
            logger.warning(blocker)
            raise ExecutionError(blocker)
        identity = ''
        if not self._git_output(['config', 'user.email']):
            identity = '-c user.name=NKAS-Updater -c user.email=nkas-updater@localhost '
        self.execute(f'"{self.git}" {identity}merge --no-edit {ref}')
        logger.hr('Show Version', 1)
        self.execute(f'"{self.git}" --no-pager log -1')

    def _is_up_to_date(self, source, branch) -> bool:
        """
        Fast path check: local HEAD already matches the remote branch tip and
        no tracked file is modified, so fetch/reset would be no-ops.
        Costs one ls-remote round trip instead of fetch + reset.
        """
        if not os.path.exists('./.git'):
            return False
        local = self._git_output(['rev-parse', 'HEAD'])
        if not local:
            return False
        remote = self._git_output(['ls-remote', source, branch])
        remote_sha = remote.split()[0] if remote else ''
        if not remote_sha:
            # Network failure: fall through to the full flow, which fails at
            # fetch with the usual error handling.
            return False
        if local != remote_sha:
            return False
        # reset --hard only reverts tracked files, so untracked files don't
        # block the fast path (-uno also keeps status fast).
        if self._git_output(['status', '--porcelain', '-uno']):
            logger.info('Local tracked changes detected, force reset to remote')
            return False
        return True

    def git_repository_init(self, repo, source='origin', branch='master', proxy=''):
        """
        初始化Git
        """
        logger.hr('Git Repository Init', 1)
        if not self.execute(f'"{self.git}" init', allow_failure=True):
            self.remove('./.git/config')
            self.remove('./.git/index')
            self.remove('./.git/HEAD')
            self.execute(f'"{self.git}" init')

        """
            设置代理
        """
        logger.hr('Set Git Proxy', 1)
        if proxy:
            self.execute(f'"{self.git}" config --local http.proxy {proxy}')
            self.execute(f'"{self.git}" config --local https.proxy {proxy}')
        else:
            self.execute(f'"{self.git}" config --local --unset http.proxy', allow_failure=True)
            self.execute(f'"{self.git}" config --local --unset https.proxy', allow_failure=True)

        """
            链接上游仓库
        """
        logger.hr('Set Git Repository', 1)
        if not self.execute(f'"{self.git}" remote set-url {source} {repo}', allow_failure=True):
            self.execute(f'"{self.git}" remote add {source} {repo}')

        """
            拉取最新上游仓库最新commit
        """
        logger.hr('Fetch Repository Branch', 1)
        if self._is_up_to_date(source, branch):
            logger.info('Local HEAD matches remote branch, skip fetch and reset')
            return
        self.execute(f'"{self.git}" fetch --depth=20 {source} {branch}')

        # Remove git lock
        lock_file = './.git/index.lock'
        if os.path.exists(lock_file):
            logger.info(f'Lock file {lock_file} exists, removing')
            os.remove(lock_file)

        """
            合并到本地
        """
        # reset --hard 已将工作区对齐到 {source}/{branch}，
        # 之后再 pull --ff-only 只是重复一次网络往返，不再执行
        self.execute(f'"{self.git}" reset --hard {source}/{branch}')

        logger.hr('Show Version', 1)
        self.execute(f'"{self.git}" --no-pager log --no-merges -1')

    def git_update(self):
        logger.hr('Update Repository', 0)

        # Check if git executable exists, if not, download and install
        if not os.path.exists(self.git):
            logger.info(f'Git executable not found: {self.git}')
            self.git_install()

        source = self.update_source()
        if self.use_merge_update(source, self.Branch):
            self.git_merge_update(source, self.Branch)
            return

        self.git_repository_init(
            repo=self.Repository,
            source='origin',
            branch=self.Branch,
            proxy=self.GitProxy,
        )

    def download_file(self, urls, target_path):
        """从多个镜像地址下载，直到成功"""
        for url in urls:
            logger.info(f'Trying to download: {url}')
            try:
                with urllib.request.urlopen(url, timeout=60) as resp, open(target_path, 'wb') as f:
                    shutil.copyfileobj(resp, f)
                logger.info(f'Download successful: {url}')
                return True
            except Exception as e:
                logger.error(f'Download failed: {url} - {e}')
        return False

    def extract_with_7za(self, sfx_file, output_dir):
        """调用 7za.exe 解压 SFX 文件"""
        seven_zip = os.path.abspath('toolkit/7z/7za.exe')
        if not os.path.exists(seven_zip):
            logger.error(f'Cannot find {seven_zip}, please ensure 7za.exe is placed in toolkit directory')
            raise FileNotFoundError(f'7za.exe not found at {seven_zip}')

        sfx_file = os.path.abspath(sfx_file)
        output_dir = os.path.abspath(output_dir)

        cmd = f'"{seven_zip}" x "{sfx_file}" -o"{output_dir}" -y'
        logger.info(f'Executing extraction command: {cmd}')
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f'Extraction failed: {result.stderr}')
                raise ExecutionError(f'Extraction failed: {result.stderr}')
        except Exception as e:
            logger.error(f'Extraction error: {e}')
            raise ExecutionError(f'Extraction error: {e}')

    def get_latest_portablegit_urls(self):
        """
        从清华源获取最新 PortableGit 版本号，并拼接清华源和 GitHub 的下载链接
        返回 [清华源URL, GitHubURL]，失败时返回 []
        """
        import re
        import urllib.request

        tsinghua_url = 'https://mirrors.tuna.tsinghua.edu.cn/github-release/git-for-windows/git/LatestRelease/'
        logger.info(f'Fetching latest release info from {tsinghua_url}')
        try:
            with urllib.request.urlopen(tsinghua_url, timeout=30) as resp:
                html = resp.read().decode('utf-8', errors='ignore')

            # 匹配 PortableGit-<version>-64-bit.7z.exe
            match = re.search(r'PortableGit-(\d+(?:\.\d+)+)-64-bit\.7z\.exe', html)
            if not match:
                logger.warning('Failed to detect latest PortableGit version from Tsinghua mirror')
                return []

            version = match.group(1)  # e.g. 2.51.0 / 2.51 / 2.51.0.1
            logger.info(f'Detected latest PortableGit version: {version}')

            # 清华源链接
            tuna_url = f'{tsinghua_url}PortableGit-{version}-64-bit.7z.exe'

            # GitHub 官方链接
            github_url = f'https://github.com/git-for-windows/git/releases/download/v{version}.windows.1/PortableGit-{version}-64-bit.7z.exe'

            return [tuna_url, github_url]
        except Exception as e:
            logger.error(f'Error while fetching Tsinghua mirror info: {e}')
            return []

    def git_install(self):
        logger.hr('Install Git', 0)

        # 如果 git 可执行文件已存在，直接跳过
        if os.path.exists(self.git):
            logger.info(f'Git executable already exists: {self.git}')
            return

        target_dir = os.path.abspath('./toolkit/Git')
        os.makedirs(target_dir, exist_ok=True)

        # 默认 URL 列表（回退用）
        urls = [
            'https://github.com/git-for-windows/git/releases/download/v2.50.1.windows.1/PortableGit-2.50.1-64-bit.7z.exe',
        ]

        # 尝试获取最新 PortableGit 的清华源和 GitHub 链接
        latest_urls = self.get_latest_portablegit_urls()
        if latest_urls:
            # 插到前两位
            urls = latest_urls + urls

        exe_path = os.path.abspath(os.path.join(target_dir, 'PortableGit.exe'))

        try:
            # 下载
            if not self.download_file(urls, exe_path):
                logger.error('All mirrors failed to download, please check your network connection.')
                raise ExecutionError('Failed to download Git')

            logger.info('Starting extraction of PortableGit...')
            self.extract_with_7za(exe_path, target_dir)

            # PortableGit 的内部目录会变（2.56 起 mingw64 改为 ucrt64），不检查的话 git_update 会重复下载
            if not os.path.exists(self.git):
                logger.error(
                    f'Git installed to {target_dir}, but GitExecutable not found: {self.git}. '
                    f"Set GitExecutable in config/deploy.yaml to './toolkit/Git/cmd/git.exe'"
                )
                raise ExecutionError(f'Git executable not found after install: {self.git}')

            logger.info(f'Git installed successfully to {target_dir}')
        finally:
            # 删除临时文件
            if os.path.exists(exe_path):
                try:
                    os.remove(exe_path)
                    logger.info(f'Removed temporary file: {exe_path}')
                except Exception as e:
                    logger.error(f'Failed to remove temporary file {exe_path}: {e}')
