from __future__ import annotations

import os
import subprocess

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_HELM_REPOSITORY_VERSION = '1.0'

# Genuine kubernetes.core helm_repository argspec aliases (the module plus
# module_utils/helm_args_common.py), folded onto canonical names at the top of
# run(). Keep in sync with the pinned kubernetes.core version (requirements.yml).
_ARG_ALIASES = {
    'name': 'repo_name', 'url': 'repo_url',
    'username': 'repo_username', 'password': 'repo_password',
    'state': 'repo_state', 'force': 'force_update',
    'skip_tls_certs_check': 'insecure_skip_tls_verify',
    'kube_context': 'context', 'kubeconfig_path': 'kubeconfig',
    'ssl_ca_cert': 'ca_cert', 'verify_ssl': 'validate_certs',
}


# Arguments the in-process fast path honours (canonical names; aliases are
# folded first). Anything else delegates to the genuine collection so its
# behaviour is preserved rather than silently ignored.
_SUPPORTED_ARGS = frozenset({
    'repo_name', 'repo_url', 'repo_username', 'repo_password', 'repo_state',
    'pass_credentials', 'insecure_skip_tls_verify', 'force_update', 'ca_cert',
    'binary_path',
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
    """Import the genuine kubernetes.core helm_repository action plugin class for delegation.

    These fast plugins live in the separate `aflp.kubernetes_core` collection and
    are routed onto the `kubernetes.core.helm_repository` action by the
    `aflp_kubernetes_redirect` callback, so this import resolves to the *genuine*
    collection (a real installed dependency), never back to ourselves. Returns
    None only when that collection is absent, in which case the caller fails with
    an actionable message.
    """
    try:
        from ansible_collections.kubernetes.core.plugins.action.helm_repository import ActionModule as _Standard
    except ImportError:
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
            'fast_helm_repository v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_HELM_REPOSITORY_VERSION,
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
                display.debug('fast_helm_repository: unsupported args %r, delegating' % sorted(unsupported))
            _strict_guard(conn, self._play_context, extra_reasons=extra_reasons)
            display.debug('fast_helm_repository: non-local/become/unsupported args, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is not None:
                std = _Standard(
                    self._task, conn, self._play_context,
                    self._loader, self._templar, self._shared_loader_obj,
                )
                return std.run(task_vars=task_vars)
            if not _is_local(conn):
                return dict(failed=True, msg=(
                    'kubernetes.core.helm_repository is provided by the fast local '
                    'override, which only supports local connections, and the genuine '
                    'kubernetes.core collection is not installed to delegate to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine kubernetes.core collection.'
                ))
            if unsupported:
                return dict(failed=True, msg=(
                    'kubernetes.core.helm_repository: arguments not supported by the fast local '
                    'override (%s), and the genuine kubernetes.core collection is not '
                    'installed to delegate to.' % ', '.join(sorted(unsupported))))
            display.warning(
                'fast_helm_repository: become cannot be honoured because no genuine '
                'kubernetes.core collection is installed; running helm as the '
                'connecting user instead of the become user'
            )

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_helm_repository: local connection, calling helm directly')

        name = args.get('name') or args.get('repo_name')
        binary_path = args.get('binary_path') or 'helm'
        state = args.get('repo_state', 'present')

        if not name:
            return dict(failed=True, msg='name is required')
        if state == 'absent':
            return self._remove_repo(result, name, binary_path)
        if state != 'present':
            return dict(failed=True, msg='unsupported repo_state: %s' % state)

        repo_url = args.get('repo_url')
        if not repo_url:
            return dict(failed=True, msg='repo_url is required when repo_state is present')

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

    def _remove_repo(self, result, name, binary_path):
        cmd = [binary_path, 'repo', 'remove', name]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            return dict(failed=True, msg='failed to run helm: %s' % to_native(e))

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            # helm exits non-zero when the repo is already gone; that is the
            # desired state, not a failure (matches the genuine module, which
            # reports changed=false for an absent repo).
            if 'no repositories configured' in stderr or 'no repo named' in stderr:
                result.update(dict(changed=False, repo_name=name))
                return result
            return dict(
                failed=True,
                msg='helm repo remove failed: %s' % stderr,
                stdout=proc.stdout,
                stderr=proc.stderr,
                rc=proc.returncode,
            )

        result.update(dict(
            changed=True,
            repo_name=name,
            stdout=proc.stdout,
            stderr=proc.stderr,
            rc=proc.returncode,
        ))
        return result
