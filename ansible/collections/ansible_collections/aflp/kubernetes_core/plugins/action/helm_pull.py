from __future__ import annotations

import os
import subprocess

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_HELM_PULL_VERSION = '1.0'

# Genuine kubernetes.core helm_pull argspec aliases, folded onto canonical names
# at the top of run(). Keep in sync with the pinned kubernetes.core version.
_ARG_ALIASES = {
    'url': 'repo_url', 'chart_repo_url': 'repo_url',
    'username': 'repo_username', 'chart_repo_username': 'repo_username',
    'password': 'repo_password', 'chart_repo_password': 'repo_password',
    'insecure_skip_tls_verify': 'skip_tls_certs_check',
}


# Arguments the in-process fast path honours (canonical names; aliases are
# folded first) — the genuine module's full argspec. Anything else (a typo or
# a future kubernetes.core parameter) delegates to the genuine module so its
# behaviour is preserved rather than silently ignored.
_SUPPORTED_ARGS = frozenset({
    'chart_ref', 'chart_version', 'verify_chart', 'verify_chart_keyring',
    'provenance', 'repo_url', 'repo_username', 'repo_password',
    'pass_credentials', 'skip_tls_certs_check', 'chart_devel', 'untar_chart',
    'destination', 'chart_ca_cert', 'chart_ssl_cert_file',
    'chart_ssl_key_file', 'binary_path',
})


def _bool_arg(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


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


def _strict_guard(connection, play_context, extra_reasons=None):
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
        if extra_reasons:
            if isinstance(extra_reasons, str):
                extra_reasons = [extra_reasons]
            reasons.extend(extra_reasons)
        reason = ', '.join(reasons) or 'an unsupported argument'
        from ansible.errors import AnsibleActionFail
        raise AnsibleActionFail(
            'AFLP_STRICT: this kubernetes.core task would fall back to the genuine '
            'collection (%s); refusing because AFLP_STRICT is set.' % reason)


def mark_fast_result(result):
    """Stamp a successful fast-path result so tests can confirm the in-process
    override handled the task (the delegated fallback returns it absent)."""
    if isinstance(result, dict) and not result.get('failed'):
        result.setdefault('__produced_by_fast_plugin', True)
    return result


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        del tmp

        conn = self._connection
        args = dict(self._task.args)
        for alias, canonical in _ARG_ALIASES.items():
            if alias in args and canonical not in args:
                args[canonical] = args.pop(alias)

        display.debug(
            'fast_helm_pull v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_HELM_PULL_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        unsupported = set(args) - _SUPPORTED_ARGS
        if not _is_local(conn) or self._play_context.become or unsupported:
            extra_reasons = None
            if unsupported:
                extra_reasons = ['unsupported arguments: %s' % ', '.join(sorted(unsupported))]
                display.debug('fast_helm_pull: unsupported args %r, delegating' % sorted(unsupported))
            _strict_guard(conn, self._play_context, extra_reasons=extra_reasons)
            # The genuine kubernetes.core.helm_pull is module-only (no action
            # plugin), so unlike the other overrides we delegate via the module
            # rather than instantiating a standard action class. module_name
            # defaults to self._task.action ('kubernetes.core.helm_pull'), which
            # module_loader resolves to the genuine module (the redirect only
            # touches action_loader, not module_loader).
            display.debug('fast_helm_pull: non-local or become, delegating to genuine module')
            return self._execute_module(task_vars=task_vars,
                                        wrap_async=self._task.async_val)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_helm_pull: local connection, calling helm directly')

        chart_ref = args.get('chart_ref')
        binary_path = args.get('binary_path') or 'helm'
        destination = args.get('destination')

        if not chart_ref:
            return dict(failed=True, msg='chart_ref is required')
        if not destination:
            # Required by the genuine module's argspec; enforced here so the
            # fast path never accepts a task the fallback would reject.
            return dict(failed=True, msg='destination is required')

        cmd = [binary_path, 'pull', chart_ref, '--destination', destination]

        if args.get('chart_version'):
            cmd += ['--version', args['chart_version']]
        if args.get('repo_url'):
            cmd += ['--repo', args['repo_url']]
        if args.get('repo_username'):
            cmd += ['--username', args['repo_username']]
        if args.get('repo_password'):
            cmd += ['--password', args['repo_password']]
        if _bool_arg(args.get('pass_credentials')):
            cmd.append('--pass-credentials')
        if _bool_arg(args.get('provenance')):
            cmd.append('--prov')
        if _bool_arg(args.get('verify_chart')):
            cmd.append('--verify')
        if args.get('verify_chart_keyring'):
            cmd += ['--keyring', args['verify_chart_keyring']]
        if _bool_arg(args.get('chart_devel')):
            cmd.append('--devel')
        if _bool_arg(args.get('skip_tls_certs_check')) or _bool_arg(args.get('insecure_skip_tls_verify')):
            cmd.append('--insecure-skip-tls-verify')
        if _bool_arg(args.get('untar_chart')):
            cmd.append('--untar')
        if args.get('chart_ca_cert'):
            cmd += ['--ca-file', args['chart_ca_cert']]
        if args.get('chart_ssl_cert_file'):
            cmd += ['--cert-file', args['chart_ssl_cert_file']]
        if args.get('chart_ssl_key_file'):
            cmd += ['--key-file', args['chart_ssl_key_file']]

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
