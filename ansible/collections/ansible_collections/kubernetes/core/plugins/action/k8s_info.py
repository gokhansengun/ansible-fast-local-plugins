from __future__ import annotations

import json
import subprocess

from ansible.module_utils._text import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_K8S_INFO_VERSION = '1.0'


def _is_local(connection):
    if getattr(connection, 'transport', None) == 'local':
        return True
    load_name = getattr(connection, '_load_name', '') or ''
    if load_name == 'local' or load_name.endswith('.local'):
        return True
    if 'connection.local' in type(connection).__module__:
        return True
    return False


def _load_standard_action():
    """Import the genuine kubernetes.core k8s_info action plugin class.

    Returns None when the import resolves back to this override: this
    collection is typically installed *as* kubernetes.core (shadowing the
    genuine collection, which need not be installed at all), so delegating
    would mean the plugin instantiating itself until the interpreter
    recursion limit ('maximum recursion depth exceeded').
    """
    from ansible_collections.kubernetes.core.plugins.action.k8s_info import ActionModule as _Standard
    if getattr(_Standard, '_FAST_LOCAL_OVERRIDE', None) is True:
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
                 field_selectors, kubeconfig, context, binary_path):
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

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run kubectl: %s' % to_native(e))

    if proc.returncode != 0:
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
            'fast_k8s_info v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_K8S_INFO_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become:
            display.debug('fast_k8s_info: non-local or become, delegating to collection plugin')
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
                    'which only supports local connections, and no genuine '
                    'kubernetes.core collection is installed to fall back to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine collection ahead of this override.'
                ))
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
                binary_path=args.get('binary_path') or 'kubectl',
            )
        except RuntimeError as e:
            return dict(failed=True, msg=to_native(e))

        result.update(dict(changed=False, resources=resources))
        return result
