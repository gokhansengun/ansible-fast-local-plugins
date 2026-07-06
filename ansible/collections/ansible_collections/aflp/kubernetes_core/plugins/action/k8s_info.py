from __future__ import annotations

import json
import os
import subprocess

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_K8S_INFO_VERSION = '1.0'

# Genuine kubernetes.core k8s_info argspec aliases (module_utils/args_common.py),
# folded onto canonical names at the top of run(). Keep in sync with the pinned
# kubernetes.core version (requirements.yml).
_ARG_ALIASES = {
    'api': 'api_version', 'version': 'api_version',
    'verify_ssl': 'validate_certs', 'ssl_ca_cert': 'ca_cert',
    'cert_file': 'client_cert', 'key_file': 'client_key',
}


# Arguments the in-process fast path honours (canonical names; aliases are
# folded first). Anything else delegates to the genuine collection so its
# behaviour is preserved rather than silently ignored. validate_certs is
# conditional (an explicit true also delegates).
_SUPPORTED_ARGS = frozenset({
    'kind', 'api_version', 'name', 'namespace', 'label_selectors',
    'field_selectors', 'kubeconfig', 'context', 'binary_path', 'validate_certs',
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


def _load_standard_action():
    """Import the genuine kubernetes.core k8s_info action plugin class for delegation.

    These fast plugins live in the separate `aflp.kubernetes_core` collection and
    are routed onto the `kubernetes.core.k8s_info` action by the
    `aflp_kubernetes_redirect` callback, so this import resolves to the *genuine*
    collection (a real installed dependency), never back to ourselves. Returns
    None only when that collection is absent, in which case the caller fails with
    an actionable message.
    """
    try:
        from ansible_collections.kubernetes.core.plugins.action.k8s_info import ActionModule as _Standard
    except ImportError:
        return None
    return _Standard


def _resource_type(kind, api_version):
    """Return the kubectl resource type string for the given kind and api_version.

    Core API resources (api_version='v1') are referenced by lowercase kind alone.
    Non-core resources are referenced as 'kind.group' so kubectl routes to the
    correct API group without relying on short-name aliases.
    """
    if '/' in api_version:
        group = api_version.split('/')[0]
        return '%s.%s' % (kind.lower(), group)
    return kind.lower()


def _kubectl_get(kind, api_version, name, namespace, label_selectors,
                 field_selectors, kubeconfig, context, binary_path, insecure=False):
    """Run kubectl get and return parsed JSON. Raises RuntimeError on failure."""
    cmd = [binary_path, 'get', _resource_type(kind, api_version), '--output=json']

    if name:
        cmd.append(name)
    if namespace:
        cmd += ['--namespace', namespace]
    if label_selectors:
        cmd += ['--selector', ','.join(label_selectors)]
    if field_selectors:
        cmd += ['--field-selector', ','.join(field_selectors)]
    if kubeconfig:
        cmd += ['--kubeconfig', kubeconfig]
    if context:
        cmd += ['--context', context]
    if insecure:
        cmd.append('--insecure-skip-tls-verify')

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run kubectl: %s' % to_native(e))

    if proc.returncode != 0:
        # A NotFound for a named resource is not an error: genuine k8s_info
        # returns an empty list so callers can do `until: r.resources | length == 0`.
        # The API status reason '(NotFound)' is specific to a missing resource and
        # won't match unrelated failures (e.g. a missing kubeconfig file).
        if '(NotFound)' in (proc.stderr or ''):
            return []
        raise RuntimeError(proc.stderr.strip())

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError('failed to parse kubectl output: %s' % to_native(e))

    # kubectl returns a List when no name is given, a single object when name is given.
    return data.get('items', [data])


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
            'fast_k8s_info v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_K8S_INFO_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        unsupported = set(args) - _SUPPORTED_ARGS
        # validate_certs=false maps onto kubectl --insecure-skip-tls-verify; an
        # explicit true would have to *enforce* verification over whatever the
        # kubeconfig says, which only the genuine client can do.
        if _bool_arg(args.get('validate_certs')):
            unsupported = unsupported | {'validate_certs'}
        if not _is_local(conn) or self._play_context.become or unsupported:
            extra_reasons = None
            if unsupported:
                extra_reasons = ['unsupported arguments: %s' % ', '.join(sorted(unsupported))]
                display.debug('fast_k8s_info: unsupported args %r, delegating' % sorted(unsupported))
            _strict_guard(conn, self._play_context, extra_reasons=extra_reasons)
            display.debug('fast_k8s_info: non-local/become/unsupported args, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is not None:
                std = _Standard(
                    self._task, conn, self._play_context,
                    self._loader, self._templar, self._shared_loader_obj,
                )
                return std.run(task_vars=task_vars)
            if not _is_local(conn):
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s_info is provided by the fast local override, '
                    'which only supports local connections, and the genuine '
                    'kubernetes.core collection is not installed to delegate to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine kubernetes.core collection.'
                ))
            if unsupported:
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s_info: arguments not supported by the fast local '
                    'override (%s), and the genuine kubernetes.core collection is not '
                    'installed to delegate to.' % ', '.join(sorted(unsupported))))
            display.warning(
                'fast_k8s_info: become cannot be honoured because no genuine '
                'kubernetes.core collection is installed; continuing with the '
                'local kubectl fast path (become has no effect on API queries)'
            )

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_k8s_info: local connection, calling kubectl directly')

        kind = args.get('kind')
        if not kind:
            return dict(failed=True, msg='kind is required')

        try:
            resources = _kubectl_get(
                kind=kind,
                api_version=args.get('api_version', 'v1'),
                name=args.get('name'),
                namespace=args.get('namespace'),
                label_selectors=args.get('label_selectors') or [],
                field_selectors=args.get('field_selectors') or [],
                kubeconfig=args.get('kubeconfig'),
                context=args.get('context'),
                insecure=_bool_arg(args.get('validate_certs')) is False,
                binary_path=args.get('binary_path') or 'kubectl',
            )
        except RuntimeError as e:
            return dict(failed=True, msg=to_native(e))

        result.update(dict(changed=False, resources=resources))
        return result
