from __future__ import annotations

import os
import subprocess

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_HELM_PULL_VERSION = '1.0'


def _is_local(connection):
    # AFLP_DISABLE kill-switch: force fallback to the genuine kubernetes.core plugin.
    if os.environ.get('AFLP_DISABLE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return False
    if getattr(connection, 'transport', None) == 'local':
        return True
    load_name = getattr(connection, '_load_name', '') or ''
    if load_name == 'local' or load_name.endswith('.local'):
        return True
    if 'connection.local' in type(connection).__module__:
        return True
    return False


def _strict_guard(connection, play_context):
    # AFLP_STRICT: raise rather than fall back to the genuine collection, so an
    # unintended k8s task in a local-only run is caught. AFLP_DISABLE (the global
    # kill-switch) is an intentional fallback and suppresses this.
    truthy = ('1', 'true', 'yes', 'on')
    if (os.environ.get('AFLP_STRICT', '').strip().lower() in truthy
            and os.environ.get('AFLP_DISABLE', '').strip().lower() not in truthy):
        reasons = []
        if not _is_local(connection):
            reasons.append('non-local connection')
        if getattr(play_context, 'become', False):
            reasons.append('become')
        reason = ', '.join(reasons) or 'an unsupported argument'
        from ansible.errors import AnsibleActionFail
        raise AnsibleActionFail(
            'AFLP_STRICT: this kubernetes.core task would fall back to the genuine '
            'collection (%s); refusing because AFLP_STRICT is set.' % reason)


def _load_standard_action():
    """Import the genuine kubernetes.core helm_pull action plugin class.

    Returns None when the import resolves back to this override: this
    collection is typically installed *as* kubernetes.core (shadowing the
    genuine collection, which need not be installed at all), so delegating
    would mean the plugin instantiating itself until the interpreter
    recursion limit ('maximum recursion depth exceeded').
    """
    from ansible_collections.kubernetes.core.plugins.action.helm_pull import ActionModule as _Standard
    if getattr(_Standard, '_FAST_LOCAL_OVERRIDE', None) is True:
        return None
    return _Standard


def mark_fast_result(result):
    """Stamp a successful fast-path result so tests can confirm the in-process
    override handled the task (the delegated fallback returns it absent)."""
    if isinstance(result, dict) and not result.get('failed'):
        result.setdefault('__produced_by_fast_plugin', True)
    return result


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    # Marks this class (and any separately-loaded copy of this file) as the
    # fast override so _load_standard_action can detect self-shadowing.
    _FAST_LOCAL_OVERRIDE = True

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        conn = self._connection
        args = self._task.args

        display.debug(
            'fast_helm_pull v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_HELM_PULL_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become:
            _strict_guard(conn, self._play_context)
            display.debug('fast_helm_pull: non-local or become, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is not None:
                std = _Standard(
                    self._task, conn, self._play_context,
                    self._loader, self._templar, self._shared_loader_obj,
                )
                return std.run(task_vars=task_vars)
            if not _is_local(conn):
                return dict(failed=True, msg=(
                    'kubernetes.core.helm_pull is provided by the fast local '
                    'override, which only supports local connections, and no genuine '
                    'kubernetes.core collection is installed to fall back to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine collection ahead of this override.'
                ))
            display.warning(
                'fast_helm_pull: become cannot be honoured because no genuine '
                'kubernetes.core collection is installed; running helm as the '
                'connecting user instead of the become user'
            )

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_helm_pull: local connection, calling helm directly')

        chart_ref = args.get('chart_ref')
        binary_path = args.get('binary_path') or 'helm'

        if not chart_ref:
            return dict(failed=True, msg='chart_ref is required')

        cmd = [binary_path, 'pull', chart_ref]

        if args.get('chart_version'):
            cmd += ['--version', args['chart_version']]
        if args.get('repo_url'):
            cmd += ['--repo', args['repo_url']]
        if args.get('repo_username'):
            cmd += ['--username', args['repo_username']]
        if args.get('repo_password'):
            cmd += ['--password', args['repo_password']]
        if args.get('pass_credentials'):
            cmd.append('--pass-credentials')
        if args.get('provenance'):
            cmd.append('--prov')
        if args.get('verify_chart'):
            cmd.append('--verify')
        if args.get('verify_chart_keyring'):
            cmd += ['--keyring', args['verify_chart_keyring']]
        if args.get('chart_devel'):
            cmd.append('--devel')
        if args.get('skip_tls_certs_check') or args.get('insecure_skip_tls_verify'):
            cmd.append('--insecure-skip-tls-verify')
        if args.get('untar'):
            cmd.append('--untar')
        if args.get('untar_dir'):
            cmd += ['--untardir', args['untar_dir']]
        if args.get('chart_destination') or args.get('destination'):
            cmd += ['--destination', args.get('chart_destination') or args['destination']]
        if args.get('ca_cert'):
            cmd += ['--ca-file', args['ca_cert']]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            return dict(failed=True, msg='failed to run helm: %s' % to_native(e))

        if proc.returncode != 0:
            return dict(
                failed=True,
                msg='helm pull failed: %s' % proc.stderr.strip(),
                command=' '.join(cmd),
                stdout=proc.stdout,
                stderr=proc.stderr,
                rc=proc.returncode,
            )

        result.update(dict(
            changed=True,
            command=' '.join(cmd),
            stdout=proc.stdout,
            stderr=proc.stderr,
            rc=proc.returncode,
        ))
        return result
