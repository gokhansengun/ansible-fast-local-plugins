from __future__ import annotations

import json
import subprocess

import yaml

from ansible.module_utils._text import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_K8S_VERSION = '1.0'


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
    """Import the genuine kubernetes.core k8s action plugin class.

    Returns None when the import resolves back to this override: this
    collection is typically installed *as* kubernetes.core (shadowing the
    genuine collection, which need not be installed at all), so delegating
    would mean the plugin instantiating itself until the interpreter
    recursion limit ('maximum recursion depth exceeded').
    """
    from ansible_collections.kubernetes.core.plugins.action.k8s import ActionModule as _Standard
    if getattr(_Standard, '_FAST_LOCAL_OVERRIDE', None) is True:
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


def _kubectl_diff(manifest_str, src, kubeconfig, context, binary_path):
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
                   kubeconfig, context, binary_path):
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
                    field_selectors, kubeconfig, context, binary_path):
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

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run kubectl delete: %s' % to_native(e))

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    return bool(proc.stdout.strip())


def _kubectl_patch(kind, api_version, name, namespace, patch_data, merge_type,
                   kubeconfig, context, binary_path):
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


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    # Marks this class (and any separately-loaded copy of this file) as the
    # fast override so _load_standard_action can detect self-shadowing.
    _FAST_LOCAL_OVERRIDE = True

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
        args = self._task.args

        display.debug(
            'fast_k8s v%s: transport=%r  _load_name=%r  class=%s.%s' % (
                FAST_K8S_VERSION,
                getattr(conn, 'transport', 'N/A'),
                getattr(conn, '_load_name', 'N/A'),
                type(conn).__module__,
                type(conn).__name__,
            )
        )

        if not _is_local(conn) or self._play_context.become:
            display.debug('fast_k8s: non-local or become, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is not None:
                return self._delegate(task_vars, _Standard)
            if not _is_local(conn):
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s is provided by the fast local override, '
                    'which only supports local connections, and no genuine '
                    'kubernetes.core collection is installed to fall back to. '
                    'Run the task on the controller (e.g. delegate_to: localhost) '
                    'or install the genuine collection ahead of this override.'
                ))
            display.warning(
                'fast_k8s: become cannot be honoured because no genuine '
                'kubernetes.core collection is installed; continuing with the '
                'local kubectl fast path (become has no effect on API calls)'
            )

        # wait/template require complex logic not worth reimplementing; delegate.
        if args.get('wait') or args.get('wait_condition') or args.get('template'):
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
        if args.get('apply') is False:
            display.debug('fast_k8s: apply=false, delegating to collection plugin')
            _Standard = _load_standard_action()
            if _Standard is None:
                return dict(failed=True, msg=(
                    'kubernetes.core.k8s: apply=false is not supported by the '
                    'fast local override and no genuine kubernetes.core '
                    'collection is installed to fall back to'
                ))
            return self._delegate(task_vars, _Standard)

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
        binary_path = args.get('binary_path') or 'kubectl'
        force = bool(args.get('force', False))
        label_selectors = args.get('label_selectors') or []
        field_selectors = args.get('field_selectors') or []
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
                    binary_path=binary_path,
                )
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))

            result.update(dict(changed=True, result=patched))
            return result

        return dict(failed=True, msg='unsupported state: %s' % state)
