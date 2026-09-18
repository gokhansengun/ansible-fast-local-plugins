v0.5.0 - Drop the py3.12 / ansible-core 2.19.3 / hashivault 5.4.0 pair; remove pre-2.19 `_task`/`_result` fallback
v0.4.5 - Count loop items from the per-item ok hook; ansible-core 2.21 hides `skipped`, so skips counted as fallbacks
v0.4.4 - Stream the fast fetch copy with shutil.copyfile; buffering the whole file OOM-killed the controller
v0.4.3 - Use create/patch/replace for state=present instead of kubectl apply
v0.4.2 - Report a missing secret the way stock does for hashivault_read
v0.4.1 - Fix regressions with aliases
v0.4.0 - Honor stock argspec aliases and gate unsupported args in fast plugins
v0.3.0 - copy: recursive directory src on the fast path; command/shell: mark non-zero rc
v0.2.1 - Add helm_pull and helm_info kubernetes.core overrides
v0.2.0 - Add strict mode to fail when a fast plugin needs fallback
v0.1.6 - Route ansible.builtin.* to fast plugins and mark fast-path results
v0.1.5 - Fix issue with plugins importing original module when non-local
v0.1.4 - Add rc to hashivault_read, Jinja2 import support in template, and k8s absent-by-definition
v0.1.3 - Wrap multi-document YAML definitions in a k8s List for kubectl
v0.1.2 - Handle multi-document YAML definitions in kubernetes.core.k8s plugin
v0.1.1 - Support YAML string definitions in kubernetes.core.k8s plugin
v0.1.0 - First tagged release of the fast local action plugins
