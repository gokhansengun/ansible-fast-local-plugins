from __future__ import annotations

import json
import os
import subprocess

import yaml

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_K8S_VERSION = '1.0'

# Genuine kubernetes.core k8s argspec aliases (module_utils/args_common.py plus
# the module's own delete_all), folded onto canonical names at the top of run().
# Keep in sync with the pinned kubernetes.core version (requirements.yml).
_ARG_ALIASES = {
    'api': 'api_version', 'version': 'api_version',
    'definition': 'resource_definition', 'inline': 'resource_definition',
    'all': 'delete_all',
    'verify_ssl': 'validate_certs', 'ssl_ca_cert': 'ca_cert',
    'cert_file': 'client_cert', 'key_file': 'client_key',
}


# Arguments the in-process fast path honours (canonical names; aliases are
# folded first). Anything else delegates to the genuine collection so its
# behaviour is preserved rather than silently ignored. wait/wait_condition/
# template/apply are listed because dedicated gates below delegate them, and
# validate_certs is conditional (an explicit true also delegates).
_SUPPORTED_ARGS = frozenset({
    'state', 'kind', 'api_version', 'name', 'namespace', 'resource_definition',
    'src', 'kubeconfig', 'context', 'binary_path', 'force', 'label_selectors',
    'field_selectors', 'merge_type', 'server_side_apply', 'validate_certs',
    'wait', 'wait_condition', 'template', 'apply',
})


def _bool_arg(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _as_list(value):
    # Mirror AnsibleModule's check_type_list (type=list): a bare string is
    # comma-split, a scalar is wrapped, a list passes through. The fast path
    # skips argspec, so callers must coerce list-typed args themselves.
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in value.split(',')]
    return [value]


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
    """Import the genuine kubernetes.core k8s action plugin class for delegation.

    These fast plugins live in the separate `aflp.kubernetes_core` collection and
    are routed onto the `kubernetes.core.k8s` action by the
    `aflp_kubernetes_redirect` callback, so this import resolves to the *genuine*
    collection (a real installed dependency), never back to ourselves. Returns
    None only when that collection is absent, in which case the caller fails with
    an actionable message.
    """
    try:
        from ansible_collections.kubernetes.core.plugins.action.k8s import ActionModule as _Standard
    except ImportError:
        return None
    return _Standard


def _resource_type(kind, api_version):
    """Return the kubectl resource type string (e.g. 'deployment.apps' for apps/v1)."""
    if '/' in api_version:
        group = api_version.split('/')[0]
        return '%s.%s' % (kind.lower(), group)
    return kind.lower()


def _to_manifest_str(definition):
    """Convert a dict/list/JSON-string/YAML-string definition to a JSON string.

    Accepts dicts and lists directly, JSON strings, and YAML strings (as
    produced by lookup('template', ...) or lookup('file', ...)).
    Raises ValueError if the input cannot be parsed or does not represent a
    mapping or sequence.
    """
    if isinstance(definition, (dict, list)):
        return json.dumps(definition)
    if isinstance(definition, str):
        try:
            parsed = json.loads(definition)
            return json.dumps(parsed)
        except json.JSONDecodeError:
            pass
        try:
            docs = [d for d in yaml.safe_load_all(definition) if d is not None]
        except yaml.YAMLError:
            raise ValueError('definition string is not valid JSON or YAML')
        if not docs or not all(isinstance(d, (dict, list)) for d in docs):
            raise ValueError('definition string is not valid JSON or YAML')
        if len(docs) == 1:
            return json.dumps(docs[0])
        return json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': docs})
    raise ValueError(
        'definition must be a dict, list, or JSON string, got %s' % type(definition).__name__
    )


def _objects_from_definition(definition):
    """Return a flat list of resource dicts from a definition value.

    Handles single objects, Kubernetes List objects, and multi-document YAML
    (which _to_manifest_str already wraps in a List).
    Raises ValueError if the definition cannot be parsed.
    """
    obj = json.loads(_to_manifest_str(definition))
    if isinstance(obj, dict) and obj.get('kind') == 'List':
        return obj.get('items') or []
    return [obj]


def _kubectl_diff(manifest_str, src, kubeconfig, context, binary_path, insecure=False):
    """Run kubectl diff to detect whether applying would change cluster state.

    Returns True if there are differences (apply would change something) or
    the resource does not yet exist. Returns False when the cluster already
    matches the manifest exactly. Raises RuntimeError on unexpected errors.
    """
    cmd = [binary_path, 'diff']
    if src:
        cmd += ['-f', src]
    else:
        cmd += ['-f', '-']
    if kubeconfig:
        cmd += ['--kubeconfig', kubeconfig]
    if context:
        cmd += ['--context', context]
    if insecure:
        cmd.append('--insecure-skip-tls-verify')

    try:
        proc = subprocess.run(
            cmd,
            input=manifest_str if not src else None,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        raise RuntimeError('failed to run kubectl diff: %s' % to_native(e))

    if proc.returncode == 0:
        return False  # no differences
    if proc.returncode == 1:
        return True   # differences found
    # Any other exit code is a genuine error (e.g. permission denied, bad kubeconfig).
    raise RuntimeError('kubectl diff error (rc=%d): %s' % (proc.returncode, proc.stderr.strip()))


def _kubectl_apply(manifest_str, src, server_side, field_manager, force,
                   kubeconfig, context, binary_path, insecure=False):
    """Run kubectl apply and return the resulting resource object.

    Raises RuntimeError on failure.
    """
    cmd = [binary_path, 'apply', '--output=json']
    if src:
        cmd += ['-f', src]
    else:
        cmd += ['-f', '-']
    if server_side:
        cmd.append('--server-side')
        if field_manager:
            cmd += ['--field-manager', field_manager]
        if force:
            cmd.append('--force-conflicts')
    elif force:
        cmd.append('--force')
    if kubeconfig:
        cmd += ['--kubeconfig', kubeconfig]
    if context:
        cmd += ['--context', context]
    if insecure:
        cmd.append('--insecure-skip-tls-verify')

    try:
        proc = subprocess.run(
            cmd,
            input=manifest_str if not src else None,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        raise RuntimeError('failed to run kubectl apply: %s' % to_native(e))

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def _kubectl_delete(kind, api_version, name, namespace, label_selectors,
                    field_selectors, kubeconfig, context, binary_path, insecure=False):
    """Delete resource(s).  Returns True if something was actually deleted.

    Uses --ignore-not-found so a missing resource is not an error.
    Raises RuntimeError on unexpected kubectl failures.
    """
    cmd = [binary_path, 'delete', _resource_type(kind, api_version),
           '--ignore-not-found', '--output=name']
    if name:
        cmd.append(name)
    else:
        if label_selectors:
            cmd += ['--selector', ','.join(label_selectors)]
        if field_selectors:
            cmd += ['--field-selector', ','.join(field_selectors)]
    if namespace:
        cmd += ['--namespace', namespace]
    if kubeconfig:
        cmd += ['--kubeconfig', kubeconfig]
    if context:
        cmd += ['--context', context]
    if insecure:
        cmd.append('--insecure-skip-tls-verify')

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run kubectl delete: %s' % to_native(e))

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    return bool(proc.stdout.strip())


def _kubectl_patch(kind, api_version, name, namespace, patch_data, merge_type,
                   kubeconfig, context, binary_path, insecure=False):
    """Run kubectl patch and return the patched resource object.

    Raises RuntimeError on failure.
    """
    _type_map = {
        'strategic-merge': 'strategic',
        'merge': 'merge',
        'json': 'json',
        'strategic': 'strategic',
    }
    patch_type = _type_map.get(merge_type or 'strategic', 'strategic')
    patch_str = json.dumps(patch_data) if isinstance(patch_data, (dict, list)) else str(patch_data)

    cmd = [binary_path, 'patch', _resource_type(kind, api_version), name,
           '--patch', patch_str, '--type', patch_type, '--output=json']
    if namespace:
        cmd += ['--namespace', namespace]
    if kubeconfig:
        cmd += ['--kubeconfig', kubeconfig]
    if context:
        cmd += ['--context', context]
    if insecure:
        cmd.append('--insecure-skip-tls-verify')

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run kubectl patch: %s' % to_native(e))

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError('failed to parse kubectl patch output: %s' % to_native(e))


def mark_fast_result(result):
    """Stamp a successful fast-path result so tests can confirm the in-process
    override handled the task (the delegated fallback returns it absent)."""
    if isinstance(result, dict) and not result.get('failed'):
        result.setdefault('__produced_by_fast_plugin', True)
    return result


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def _delegate(self, task_vars, standard_cls):
        std = standard_cls(
            self._task, self._connection, self._play_context,
            self._loader, self._templar, self._shared_loader_obj,
        )
        return std.run(task_vars=task_vars)

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
            'fast_k8s v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_K8S_VERSION,
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
                display.debug('fast_k8s: unsupported args %r, delegating' % sorted(unsupported))
            _strict_guard(conn, self._play_context, extra_reasons=extra_reasons)
            display.debug('fast_k8s: non-local/become/unsupported args, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is not None:
                return self._delegate(task_vars, _Standard)
            if unsupported:
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s: arguments not supported by the fast local '
                    'override (%s), and the genuine kubernetes.core collection is not '
                    'installed to delegate to.' % ', '.join(sorted(unsupported))))
            if not _is_local(conn):
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s is provided by the fast local override, '
                    'which only supports local connections, and the genuine '
                    'kubernetes.core collection is not installed to delegate to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine kubernetes.core collection.'
                ))
            display.warning(
                'fast_k8s: become cannot be honoured because no genuine '
                'kubernetes.core collection is installed; continuing with the '
                'local kubectl fast path (become has no effect on API calls)'
            )

        # wait/template require complex logic not worth reimplementing; delegate.
        if _bool_arg(args.get('wait')) or args.get('wait_condition') or args.get('template'):
            _strict_guard(conn, self._play_context,
                          extra_reasons=['wait/wait_condition/template'])
            display.debug('fast_k8s: wait/template set, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is None:
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s: wait/wait_condition/template are not '
                    'supported by the fast local override and no genuine '
                    'kubernetes.core collection is installed to fall back to'
                ))
            return self._delegate(task_vars, _Standard)

        # apply=false means create/replace semantics which differ significantly; delegate.
        if _bool_arg(args.get('apply')) is False:
            _strict_guard(conn, self._play_context, extra_reasons=['apply=false'])
            display.debug('fast_k8s: apply=false, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is None:
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s: apply=false is not supported by the '
                    'fast local override and no genuine kubernetes.core '
                    'collection is installed to fall back to'
                ))
            return self._delegate(task_vars, _Standard)

        return mark_fast_result(self._run_local(args, result))

    def _run_local(self, args, result):
        display.debug('fast_k8s: local connection, calling kubectl directly')

        state = args.get('state', 'present')
        kind = args.get('kind')
        api_version = args.get('api_version', 'v1')
        name = args.get('name')
        namespace = args.get('namespace')
        definition = args.get('definition') or args.get('resource_definition')
        src = args.get('src')
        kubeconfig = args.get('kubeconfig')
        context = args.get('context')
        insecure = _bool_arg(args.get('validate_certs')) is False
        binary_path = args.get('binary_path') or 'kubectl'
        force = _bool_arg(args.get('force', False))
        label_selectors = _as_list(args.get('label_selectors'))
        field_selectors = _as_list(args.get('field_selectors'))
        merge_type = args.get('merge_type')

        server_side_apply = args.get('server_side_apply')
        server_side = bool(server_side_apply)
        field_manager = None
        if isinstance(server_side_apply, dict):
            field_manager = server_side_apply.get('field_manager')

        # ------------------------------------------------------------------ #
        # state: absent                                                        #
        # ------------------------------------------------------------------ #
        if state == 'absent':
            if kind:
                # Explicit kind/name/namespace — original fast path.
                try:
                    changed = _kubectl_delete(
                        kind=kind,
                        api_version=api_version,
                        name=name,
                        namespace=namespace,
                        label_selectors=label_selectors,
                        field_selectors=field_selectors,
                        kubeconfig=kubeconfig,
                        context=context,
                        insecure=insecure,
                        binary_path=binary_path,
                    )
                except RuntimeError as e:
                    return dict(failed=True, msg=to_native(e))
                result.update(dict(changed=changed, result={}))
                return result

            if definition is not None:
                # Derive what to delete from the definition (mirrors official module).
                try:
                    objects = _objects_from_definition(definition)
                except ValueError as e:
                    return dict(failed=True, msg=to_native(e))
                changed = False
                for obj in objects:
                    obj_kind = obj.get('kind')
                    obj_api_version = obj.get('apiVersion', 'v1')
                    obj_name = (obj.get('metadata') or {}).get('name')
                    obj_namespace = (obj.get('metadata') or {}).get('namespace')
                    if not obj_kind or not obj_name:
                        return dict(failed=True,
                                    msg='each definition item must have kind and metadata.name')
                    try:
                        changed = _kubectl_delete(
                            kind=obj_kind,
                            api_version=obj_api_version,
                            name=obj_name,
                            namespace=obj_namespace,
                            label_selectors=[],
                            field_selectors=[],
                            kubeconfig=kubeconfig,
                            context=context,
                        insecure=insecure,
                            binary_path=binary_path,
                        ) or changed
                    except RuntimeError as e:
                        return dict(failed=True, msg=to_native(e))
                result.update(dict(changed=changed, result={}))
                return result

            return dict(failed=True, msg='one of kind or definition is required for state=absent')

        # ------------------------------------------------------------------ #
        # state: present / latest                                             #
        # ------------------------------------------------------------------ #
        if state in ('present', 'latest'):
            if definition is None and not src and not kind:
                return dict(failed=True, msg='one of definition, src, or kind is required')

            if src:
                manifest_str = None
            elif definition is not None:
                try:
                    manifest_str = _to_manifest_str(definition)
                except ValueError as e:
                    return dict(failed=True, msg=to_native(e))
            else:
                # Build a minimal manifest from individual arguments.
                manifest = {'apiVersion': api_version, 'kind': kind}
                meta = {}
                if name:
                    meta['name'] = name
                if namespace:
                    meta['namespace'] = namespace
                if meta:
                    manifest['metadata'] = meta
                manifest_str = json.dumps(manifest)

            try:
                has_changes = _kubectl_diff(
                    manifest_str=manifest_str,
                    src=src,
                    kubeconfig=kubeconfig,
                    context=context,
                        insecure=insecure,
                    binary_path=binary_path,
                )
            except RuntimeError as e:
                # diff failure is non-fatal: assume changes and let apply decide.
                display.debug('fast_k8s: kubectl diff failed (%s), assuming changes' % to_native(e))
                has_changes = True

            if not has_changes:
                result.update(dict(changed=False, result={}))
                return result

            try:
                applied = _kubectl_apply(
                    manifest_str=manifest_str,
                    src=src,
                    server_side=server_side,
                    field_manager=field_manager,
                    force=force,
                    kubeconfig=kubeconfig,
                    context=context,
                        insecure=insecure,
                    binary_path=binary_path,
                )
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))

            result.update(dict(changed=True, result=applied))
            return result

        # ------------------------------------------------------------------ #
        # state: patched                                                       #
        # ------------------------------------------------------------------ #
        if state == 'patched':
            if not kind:
                return dict(failed=True, msg='kind is required for state=patched')
            if not name:
                return dict(failed=True, msg='name is required for state=patched')
            if definition is None:
                return dict(failed=True, msg='definition is required for state=patched')

            try:
                patch_str = _to_manifest_str(definition)
                patch_data = json.loads(patch_str)
            except ValueError as e:
                return dict(failed=True, msg=to_native(e))

            try:
                patched = _kubectl_patch(
                    kind=kind,
                    api_version=api_version,
                    name=name,
                    namespace=namespace,
                    patch_data=patch_data,
                    merge_type=merge_type,
                    kubeconfig=kubeconfig,
                    context=context,
                        insecure=insecure,
                    binary_path=binary_path,
                )
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))

            result.update(dict(changed=True, result=patched))
            return result

        return dict(failed=True, msg='unsupported state: %s' % state)
