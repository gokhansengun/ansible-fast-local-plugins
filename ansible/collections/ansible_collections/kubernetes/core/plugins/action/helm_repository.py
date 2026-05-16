from __future__ import annotations

import subprocess

from ansible.module_utils._text import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_HELM_REPOSITORY_VERSION = '1.0'


def _is_local(connection):
    if getattr(connection, 'transport', None) == 'local':
        return True
    load_name = getattr(connection, '_load_name', '') or ''
    if load_name == 'local' or load_name.endswith('.local'):
        return True
    if 'connection.local' in type(connection).__module__:
        return True
    return False


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        conn = self._connection
        args = self._task.args

        display.debug(
            'fast_helm_repository v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_HELM_REPOSITORY_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become:
            display.debug('fast_helm_repository: non-local or become, delegating to collection plugin')
            from ansible_collections.kubernetes.core.plugins.action.helm_repository import ActionModule as _Standard
            std = _Standard(
                self._task, conn, self._play_context,
                self._loader, self._templar, self._shared_loader_obj,
            )
            return std.run(task_vars=task_vars)

        display.debug('fast_helm_repository: local connection, calling helm directly')

        name = args.get('name') or args.get('repo_name')
        repo_url = args.get('repo_url')
        binary_path = args.get('binary_path') or 'helm'

        if not name or not repo_url:
            return dict(failed=True, msg='name and repo_url are required')

        cmd = [binary_path, 'repo', 'add', name, repo_url]

        if args.get('repo_username'):
            cmd += ['--username', args['repo_username']]
        if args.get('repo_password'):
            cmd += ['--password', args['repo_password']]
        if args.get('pass_credentials'):
            cmd.append('--pass-credentials')
        if args.get('insecure_skip_tls_verify'):
            cmd.append('--insecure-skip-tls-verify')
        if args.get('force_update'):
            cmd.append('--force-update')
        if args.get('ca_cert'):
            cmd += ['--ca-file', args['ca_cert']]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            return dict(failed=True, msg='failed to run helm: %s' % to_native(e))

        if proc.returncode != 0:
            return dict(
                failed=True,
                msg='helm repo add failed: %s' % proc.stderr.strip(),
                stdout=proc.stdout,
                stderr=proc.stderr,
                rc=proc.returncode,
            )

        result.update(dict(
            changed=True,
            repo_name=name,
            repo_url=repo_url,
            stdout=proc.stdout,
            stderr=proc.stderr,
            rc=proc.returncode,
        ))
        return result
