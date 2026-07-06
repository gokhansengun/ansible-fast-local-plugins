from __future__ import annotations

import json
import os
import subprocess

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_HELM_INFO_VERSION = '1.0'

# Genuine kubernetes.core helm_info argspec aliases (the module plus
# module_utils/helm_args_common.py), folded onto canonical names at the top of
# run(). Without this, `namespace:` was silently dropped and helm queried the
# default namespace. Keep in sync with the pinned kubernetes.core version.
_ARG_ALIASES = {
    'name': 'release_name', 'namespace': 'release_namespace',
    'kube_context': 'context', 'kubeconfig_path': 'kubeconfig',
    'ssl_ca_cert': 'ca_cert', 'verify_ssl': 'validate_certs',
}


# Arguments the in-process fast path honours (canonical names; aliases are
# folded first). Anything else delegates to the genuine collection so its
# behaviour is preserved rather than silently ignored.
_SUPPORTED_ARGS = frozenset({
    'release_name', 'release_namespace', 'release_state', 'get_all_values',
    'kubeconfig', 'context', 'host', 'api_key', 'validate_certs', 'binary_path',
})


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


def _load_standard_action():
    """Import the genuine kubernetes.core helm_info action plugin class for delegation.

    These fast plugins live in the separate `aflp.kubernetes_core` collection and
    are routed onto the `kubernetes.core.helm_info` action by the
    `aflp_kubernetes_redirect` callback, so this import resolves to the *genuine*
    collection (a real installed dependency), never back to ourselves. Returns
    None only when that collection is absent, in which case the caller fails with
    an actionable message.
    """
    try:
        from ansible_collections.kubernetes.core.plugins.action.helm_info import ActionModule as _Standard
    except ImportError:
        return None
    return _Standard


def _helm_global_args(args):
    """Translate the kube connection args into helm global flags."""
    cmd = []
    if args.get('release_namespace'):
        cmd += ['--namespace', args['release_namespace']]
    if args.get('kubeconfig'):
        cmd += ['--kubeconfig', args['kubeconfig']]
    if args.get('context'):
        cmd += ['--kube-context', args['context']]
    if args.get('host'):
        cmd += ['--kube-apiserver', args['host']]
    if args.get('api_key'):
        cmd += ['--kube-token', args['api_key']]
    if args.get('validate_certs') is False:
        cmd.append('--kube-insecure-skip-tls-verify')
    return cmd


def _run_helm(cmd):
    """Run a helm command, returning (rc, stdout, stderr). Raises RuntimeError on exec failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run helm: %s' % to_native(e))
    return proc.returncode, proc.stdout, proc.stderr


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
            'fast_helm_info v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_HELM_INFO_VERSION,
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
                display.debug('fast_helm_info: unsupported args %r, delegating' % sorted(unsupported))
            _strict_guard(conn, self._play_context, extra_reasons=extra_reasons)
            display.debug('fast_helm_info: non-local/become/unsupported args, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is not None:
                std = _Standard(
                    self._task, conn, self._play_context,
                    self._loader, self._templar, self._shared_loader_obj,
                )
                return std.run(task_vars=task_vars)
            if not _is_local(conn):
                return dict(failed=True, msg=(
                    'kubernetes.core.helm_info is provided by the fast local '
                    'override, which only supports local connections, and the genuine '
                    'kubernetes.core collection is not installed to delegate to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine kubernetes.core collection.'
                ))
            if unsupported:
                return dict(failed=True, msg=(
                    'kubernetes.core.helm_info: arguments not supported by the fast local '
                    'override (%s), and the genuine kubernetes.core collection is not '
                    'installed to delegate to.' % ', '.join(sorted(unsupported))))
            display.warning(
                'fast_helm_info: become cannot be honoured because no genuine '
                'kubernetes.core collection is installed; continuing with the '
                'local helm fast path (become has no effect on release queries)'
            )

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_helm_info: local connection, calling helm directly')

        release_name = args.get('release_name') or args.get('name')
        binary_path = args.get('binary_path') or 'helm'

        if not release_name:
            return dict(failed=True, msg='release_name is required')

        global_args = _helm_global_args(args)
        release_state = args.get('release_state') or ['deployed', 'failed']

        cmd = [binary_path, 'status', release_name, '--output', 'json'] + global_args
        try:
            rc, stdout, stderr = _run_helm(cmd)
        except RuntimeError as e:
            return dict(failed=True, msg=to_native(e))

        if rc != 0:
            # A missing release is not an error for helm_info: report no status.
            if 'not found' in stderr.lower():
                result.update(dict(changed=False, status=None))
                return result
            return dict(failed=True, msg='helm status failed: %s' % stderr.strip(),
                        stderr=stderr, rc=rc)

        try:
            status = json.loads(stdout)
        except json.JSONDecodeError as e:
            return dict(failed=True, msg='failed to parse helm output: %s' % to_native(e))

        # Filter by release state (helm status reports state under info.status).
        info_status = (status.get('info') or {}).get('status')
        if info_status and info_status not in release_state:
            result.update(dict(changed=False, status=None))
            return result

        if args.get('get_all_values'):
            values_cmd = [binary_path, 'get', 'values', release_name,
                          '--all', '--output', 'json'] + global_args
            try:
                v_rc, v_stdout, v_stderr = _run_helm(values_cmd)
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))
            if v_rc != 0:
                return dict(failed=True, msg='helm get values failed: %s' % v_stderr.strip(),
                            stderr=v_stderr, rc=v_rc)
            try:
                status['values'] = json.loads(v_stdout) if v_stdout.strip() else {}
            except json.JSONDecodeError as e:
                return dict(failed=True, msg='failed to parse helm values output: %s' % to_native(e))

        result.update(dict(changed=False, status=status))
        return result
