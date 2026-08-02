from __future__ import annotations

import json
import os
import subprocess

import yaml

from ansible.module_utils.common.text.converters import to_native
from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

FAST_K8S_VERSION = '1.1'

# Patch strategies kubernetes.core's update() tries, in order, when the task
# sets no merge_type.
_DEFAULT_MERGE_TYPES = ('strategic-merge', 'merge')

# diff_objects() in kubernetes.core treats a difference confined to these
# metadata keys as "no meaningful change"; _objects_match mirrors that.
_VOLATILE_METADATA = ('generation', 'resourceVersion')

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

    Handles single objects, plain lists of objects, Kubernetes List objects, and
    multi-document YAML (which _to_manifest_str already wraps in a List).
    Raises ValueError if the definition cannot be parsed.
    """
    obj = json.loads(_to_manifest_str(definition))
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and obj.get('kind') == 'List':
        return obj.get('items') or []
    return [obj]


def _resolve_objects(definition, src, kind, api_version, name, namespace):
    """Return the resource dicts a present/patched task operates on.

    Mirrors kubernetes.core's create_definitions(): the definition (or the src
    file) supplies the objects, and the task's kind/api_version/name/namespace
    fill in whatever each manifest leaves out.  With no definition and no src,
    the arguments alone build a minimal manifest.
    Raises ValueError if the definition cannot be parsed, OSError if src cannot
    be read.
    """
    if definition is not None:
        objects = _objects_from_definition(definition)
    elif src:
        # The fast path only runs on a local connection, so src is on this host.
        with open(src) as f:
            objects = _objects_from_definition(f.read())
    else:
        objects = [{}]

    resolved = []
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError('each definition item must be a mapping, got %s'
                             % type(obj).__name__)
        obj = dict(obj)
        if api_version:
            obj.setdefault('apiVersion', api_version)
        if kind:
            obj.setdefault('kind', kind)
        meta = dict(obj.get('metadata') or {})
        if name:
            meta.setdefault('name', name)
        if namespace:
            meta.setdefault('namespace', namespace)
        obj['metadata'] = meta
        resolved.append(obj)
    return resolved


def _merge_types(merge_type):
    """Return the ordered patch strategies to try.

    Mirrors kubernetes.core's update(): the task's merge_type list, or
    strategic-merge then merge when it is unset.
    """
    types = [t for t in _as_list(merge_type) if t]
    return types or list(_DEFAULT_MERGE_TYPES)


def _without_volatile_metadata(obj):
    if not isinstance(obj, dict):
        return obj
    stripped = dict(obj)
    meta = stripped.get('metadata')
    if isinstance(meta, dict):
        meta = dict(meta)
        for key in _VOLATILE_METADATA:
            meta.pop(key, None)
        stripped['metadata'] = meta
    return stripped


def _objects_match(before, after):
    """True when two versions of a resource are equivalent.

    Mirrors kubernetes.core's diff_objects(): a difference confined to
    metadata.generation / metadata.resourceVersion is not a change.
    """
    return _without_volatile_metadata(before) == _without_volatile_metadata(after)


def _kubectl_get(kind, api_version, name, namespace, kubeconfig, context,
                 binary_path, insecure=False):
    """Return the live resource as a dict, or None when it does not exist.

    Raises RuntimeError on any other kubectl failure (unknown kind, bad
    kubeconfig, forbidden, ...).
    """
    cmd = [binary_path, 'get', _resource_type(kind, api_version), name,
           '--ignore-not-found', '--output=json']
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
        raise RuntimeError('failed to run kubectl get: %s' % to_native(e))

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError('failed to parse kubectl get output: %s' % to_native(e))


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


def _kubectl_apply(manifest_str, src, server_side, field_manager, force_conflicts,
                   kubeconfig, context, binary_path, insecure=False):
    """Run kubectl apply and return the resulting resource object.

    Only reached when the task sets apply=true.  force_conflicts comes from the
    server_side_apply dict, not from the task's force argument: kubernetes.core
    ignores force in the apply path (and its force means replace, never
    kubectl apply --force, which deletes and recreates the resource).

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
        if force_conflicts:
            cmd.append('--force-conflicts')
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


def _kubectl_stdin_verb(verb, manifest_str, kubeconfig, context, binary_path,
                        insecure=False):
    """Run `kubectl <verb> -f -` with the manifest on stdin and return the object.

    Raises RuntimeError on failure.
    """
    cmd = [binary_path, verb, '-f', '-', '--output=json']
    if kubeconfig:
        cmd += ['--kubeconfig', kubeconfig]
    if context:
        cmd += ['--context', context]
    if insecure:
        cmd.append('--insecure-skip-tls-verify')

    try:
        proc = subprocess.run(cmd, input=manifest_str, capture_output=True, text=True)
    except Exception as e:
        raise RuntimeError('failed to run kubectl %s: %s' % (verb, to_native(e)))

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def _kubectl_create(manifest_str, kubeconfig, context, binary_path, insecure=False):
    """Create a resource that does not exist yet (kubernetes.core's create())."""
    return _kubectl_stdin_verb('create', manifest_str, kubeconfig, context,
                               binary_path, insecure=insecure)


def _kubectl_replace(manifest_str, kubeconfig, context, binary_path, insecure=False):
    """Overwrite an existing resource (kubernetes.core's replace(), i.e. force=true).

    This is a PUT, not a delete-and-recreate.
    """
    return _kubectl_stdin_verb('replace', manifest_str, kubeconfig, context,
                               binary_path, insecure=insecure)


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
        force_conflicts = False
        if isinstance(server_side_apply, dict):
            field_manager = server_side_apply.get('field_manager')
            force_conflicts = _bool_arg(server_side_apply.get('force_conflicts')) or False

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
        # state: present / latest / patched                                   #
        # ------------------------------------------------------------------ #
        if state in ('present', 'latest', 'patched'):
            if definition is None and not src and not kind:
                return dict(failed=True, msg='one of definition, src, or kind is required')
            if state == 'patched' and definition is None:
                return dict(failed=True, msg='definition is required for state=patched')

            # apply=true is the only mode with `kubectl apply` semantics.  For
            # everything else kubernetes.core creates/replaces/patches the
            # resource, which never prunes fields the manifest omits -- see
            # _reconcile_object.
            if _bool_arg(args.get('apply')):
                return self._run_apply(
                    result,
                    definition=definition, src=src, kind=kind,
                    api_version=api_version, name=name, namespace=namespace,
                    server_side=server_side, field_manager=field_manager,
                    force_conflicts=force_conflicts, kubeconfig=kubeconfig,
                    context=context, insecure=insecure, binary_path=binary_path,
                )

            try:
                objects = _resolve_objects(definition, src, kind, api_version,
                                           name, namespace)
            except ValueError as e:
                return dict(failed=True, msg=to_native(e))
            except OSError as e:
                return dict(failed=True, msg='failed to read src %s: %s' % (src, to_native(e)))

            results = []
            for obj in objects:
                obj_result = self._reconcile_object(
                    obj,
                    state=state,
                    force=force,
                    merge_type=merge_type,
                    kubeconfig=kubeconfig,
                    context=context,
                    insecure=insecure,
                    binary_path=binary_path,
                )
                if obj_result.get('failed'):
                    return obj_result
                results.append(obj_result)

            # Result shape mirrors kubernetes.core's run_module(): a single
            # definition returns its own result, several return a results list.
            if len(results) == 1:
                result.update(results[0])
            else:
                result.update(dict(
                    changed=any(r['changed'] for r in results),
                    result={'results': results},
                ))
            return result

        return dict(failed=True, msg='unsupported state: %s' % state)

    def _run_apply(self, result, definition, src, kind, api_version, name,
                   namespace, server_side, field_manager, force_conflicts,
                   kubeconfig, context, insecure, binary_path):
        """apply=true: hand the whole manifest to `kubectl apply` in one call."""
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
            result.update(dict(changed=False, result={}, method='apply'))
            return result

        try:
            applied = _kubectl_apply(
                manifest_str=manifest_str,
                src=src,
                server_side=server_side,
                field_manager=field_manager,
                force_conflicts=force_conflicts,
                kubeconfig=kubeconfig,
                context=context,
                insecure=insecure,
                binary_path=binary_path,
            )
        except RuntimeError as e:
            return dict(failed=True, msg=to_native(e))

        result.update(dict(changed=True, result=applied, method='apply'))
        return result

    def _reconcile_object(self, obj, state, force, merge_type, kubeconfig,
                          context, insecure, binary_path):
        """Bring one object to the desired state without `kubectl apply`.

        Mirrors kubernetes.core's perform_action(): create when the resource is
        absent, replace when force=true, otherwise patch with each merge_type in
        turn.  A patch only touches the fields the manifest carries -- unlike
        `kubectl apply`, which diffs against last-applied-configuration and
        deletes everything the manifest omits (a partial manifest applied over a
        resource created by `kubectl apply` would null out spec.selector,
        containers, ... and be rejected by the API server).
        """
        kind = obj.get('kind')
        api_version = obj.get('apiVersion') or 'v1'
        meta = obj.get('metadata') or {}
        name = meta.get('name')
        namespace = meta.get('namespace')

        if not kind:
            return dict(failed=True, msg=(
                'kind is required: set it in the definition or as a task argument'))
        if state == 'patched' and not name:
            return dict(failed=True, msg=(
                'name is required for state=patched: set metadata.name in the '
                'definition or pass the name argument'))

        existing = None
        if name:
            try:
                existing = _kubectl_get(
                    kind=kind, api_version=api_version, name=name,
                    namespace=namespace, kubeconfig=kubeconfig, context=context,
                    insecure=insecure, binary_path=binary_path,
                )
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))

        manifest_str = json.dumps(obj)

        if existing is None:
            if state == 'patched':
                # Genuine warns and leaves the cluster alone rather than creating.
                return dict(changed=False, result={}, warnings=[
                    "resource 'kind=%s,name=%s' was not found but will not be "
                    "created as 'state' parameter has been set to 'patched'"
                    % (kind, name)])
            try:
                created = _kubectl_create(
                    manifest_str=manifest_str, kubeconfig=kubeconfig,
                    context=context, insecure=insecure, binary_path=binary_path,
                )
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))
            return dict(changed=True, result=created, method='create')

        if force:
            try:
                replaced = _kubectl_replace(
                    manifest_str=manifest_str, kubeconfig=kubeconfig,
                    context=context, insecure=insecure, binary_path=binary_path,
                )
            except RuntimeError as e:
                return dict(failed=True, msg=to_native(e))
            return dict(changed=not _objects_match(existing, replaced),
                        result=replaced, method='replace')

        error = None
        for strategy in _merge_types(merge_type):
            try:
                patched = _kubectl_patch(
                    kind=kind, api_version=api_version, name=name,
                    namespace=namespace, patch_data=obj, merge_type=strategy,
                    kubeconfig=kubeconfig, context=context, insecure=insecure,
                    binary_path=binary_path,
                )
            except RuntimeError as e:
                # e.g. strategic-merge is unsupported for a CRD: try the next.
                error = e
                continue
            return dict(changed=not _objects_match(existing, patched),
                        result=patched, method='update')
        return dict(failed=True, msg=to_native(error))
