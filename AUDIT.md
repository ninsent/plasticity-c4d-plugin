# Plasticity Bridge for Cinema 4D — Audit

Audit of release **1.1.6** (commit `13563f2`). Line numbers refer to that
commit. Scope: `plasticity_c4d.pyp`, `core/`, `dialogs/`, `res/`, and how the
vendored `libs/websockets` is used (the library itself was not audited).

The reference for protocol behaviour is the Plasticity Blender Bridge,
`client.py` / `handler.py` at commit `6f96a0f` (addon version 2.2.1).

Cinema 4D API claims below were checked against Maxon's documentation and
developer forum; the sources are listed in [Sources](#sources). Anything
that could only be inferred is marked **(inferred)** and needs a manual
check in Cinema 4D (IDs `M-…`).

**Severity scale.**

| Severity | Meaning |
|---|---|
| High | Wrong data reaches Plasticity or the scene, the UI gets stuck, or the plugin can crash or hang C4D. |
| Medium | A visible malfunction with a workaround, a silent inconsistency, or a likely race. |
| Low | An edge case, a docs mismatch, or a code-quality issue that makes other bugs likely. |
| Info | A note with no defect. |

**Status** is filled in as fixes land. *Fixed* means fixed in this branch, with
the test or manual check named. *Deferred* gives a reason.

---

## Summary

| ID | Sev | Area | Finding | Status |
|---|---|---|---|---|
| F01 | High | Outbox | `MCOMMAND_JOIN` misused: multi-part cages upload only their first part | Fixed |
| F02 | High | Outbox | CSTO fallback ignores the command's result list | Fixed |
| F03 | High | Outbox | Child/cache transforms dropped on the generator path | Fixed |
| F04 | High | Outbox | Export merges all SDS children; C4D subdivides only the first | Fixed |
| F05 | High | Outbox | N-gons exported as triangle/quad fragments | Fixed |
| F06 | High | UI | Refresh stays disabled forever after a LIST error or an unreadable response | Fixed |
| F07 | High | Scene | A refacet error response creates an empty "Plasticity" root | Fixed |
| F08 | High | Threading | Events are dropped silently when the queue is full | Fixed |
| F09 | High | Threading | The main thread blocks for up to 30 s on sends and 5 s on disconnect | Fixed |
| F10 | Medium | Threading | Double-connect race; a dying connection clobbers a new one | Fixed |
| F11 | Medium | Undo | Live Link undo pollution and double tessellation with Auto-Refacet | Fixed |
| F12 | Medium | Outbox | Deformers ignored on the single-polygon fast path | Fixed |
| F13 | Medium | Import | N-gon refacet with only tris/quads stores a wrong poly-face map | Fixed |
| F14 | Medium | Normals | High-level normal path never runs; low-level scale is 32767, not 32000 | Fixed |
| F15 | Medium | Import | N-gon melt overwrites the polygon selection | Fixed |
| F16 | Medium | Docs/Outbox | README promises generator upload; other objects skipped silently | Fixed |
| F17 | Medium | Outbox | `only_visible` ignores hidden parents and layers | Fixed |
| F18 | Medium | Outbox | Duplicated Outbox objects share persistent IDs | Fixed |
| F19 | Medium | Sync | Outbox ID filter mixes the item and group ID spaces | Fixed |
| F20 | Medium | Sync | An uploaded object can be mirrored into the Inbox and never removed | Fixed |
| F21 | Medium | Import | Out-of-range indices reach `CPolygon` or raise mid-transaction | Fixed |
| F22 | Medium | Client | `on_list_complete` overwritten by auto-refresh; never cleared on error | Fixed |
| F23 | Low | Undo | Name/visibility/flag edits made without `AddUndo` | Fixed |
| F24 | Low | Undo | Selection utilities change selections without undo | Fixed |
| F25 | Low | Docs | README says face/edge tags are "recreated" on update; they are removed | Fixed |
| F26 | Low | Client | The listen loop can spin on a repeating non-close exception | Fixed |
| F27 | Low | Threading | Re-entrant dispatch while a modal dialog is open | Fixed |
| F28 | Low | Quality | "v2.1.0" comments refer to plugin 1.1.x features | Fixed |
| F29 | Low | Lifecycle | Close/reopen/exit: blocking disconnect, the "connecting" state is not shut down | Fixed |
| F30 | Medium | Loading | Generic `core`/`dialogs` packages and `libs` inserted at `sys.path[0]` | Fixed |
| F31 | Low | Import | `ResizeObject(p, v, 0)`: the third argument is undocumented in Python | Fixed (defensive) |
| F32 | Low | UI | `update_unit_scale` only touches cached roots | Fixed |
| F33 | Low | UI | The Timer re-applies ~25 gadget states and the status text every 16 ms | Fixed |
| F34 | Low | Protocol | Inconsistent PUT_SOME items are encoded without checks | Fixed |
| F35 | Low | Client | Handshake race: Refresh before the handshake event skips the upload | Fixed |
| F36 | Low | Import | Empty geometry update rewrites metadata but keeps the old mesh | Fixed |
| F37 | Low | Utilities | Corrupt poly-face map JSON raises inside utilities | Fixed |
| F38 | Low | Docs | `libs/__init__.py` mentions a compiled speedups module that is never shipped | Fixed |
| F39 | Low | Release | The v1.1.6 zip ships `.DS_Store`, `speedups.c`, `py.typed`, and no licenses | Fixed |
| F40 | Info | Protocol | Blender addon differences (DELETE count offset, char-length filenames) | No change |
| F41 | Low | Sync | Transaction DELETE never removes groups (Blender parity) | Deferred |
| F42 | Low | UI | Reopening a scene, then Refresh, resets the root scale to the dialog value | Deferred |
| F43 | Info | Dialog | `PlasticityDialog.RestoreLayout` is never called by C4D | Fixed (removed) |
| F44 | Low | Scene | Deleting the Outbox null makes the next update create a second, empty Inbox | Fixed |
| F45 | Medium | Scene | Pre-1.1.0 scenes are no longer migrated into the Inbox (regression in 1.1.6) | Fixed |
| F46 | Medium | Sync | An Inbox mesh copied into an Outbox cage stops the original from syncing | Fixed |
| F47 | Medium | Sync | Uploaded IDs are kept per session, not per file, and hide objects of other Plasticity files | Fixed |
| P01–P11 | Perf | — | See [Performance](#performance) | Fixed |

---

## High

### F01 — `MCOMMAND_JOIN` misused: multi-part cages upload only their first part

* **Where:** `core/handler.py:1947-1963` (`_combine_polygon_sources`) and `:2088-2107` (`_csto_resolve`).
* **What:** both call `SendModelingCommand(MCOMMAND_JOIN, list=[obj1, obj2, ...])`, then use `clones[0]` / `poly_objects[0]` as "the joined mesh". JOIN does not work that way. It joins the **children of the object you pass**, returns the **new** object in a result list, and leaves the inputs untouched. Maxon's docs say "Passing multiple objects into SMC will not join them."
* **Effect:** any cage that resolves to more than one polygon object uploads only its first part. Examples: a Null with several cubes, Symmetry, Boole, Array, Connect with *Weld* off.
* **Trigger:** put a Null containing two offset Cubes under an SDS in the Outbox, then Refresh. Plasticity receives a single cube.
* **Fix:** drop JOIN entirely. The cage parts are read with their matrices and merged in Python (`geometry.merge_mesh_parts`). This needs no temporary document for the common path, and it keeps each part's N-gons (F05).
* **Tests:** `test_outbox.py::test_multi_part_cage_uploads_all_parts`, `::test_join_is_never_used`.

### F02 — CSTO fallback ignores the command's result list

* **Where:** `core/handler.py:2026-2083`.
* **What:** `MCOMMAND_CURRENTSTATETOOBJECT` returns a **list of new objects** and leaves the input unchanged. The code only checks `isinstance(result, BaseObject)`, so it never takes this branch. It then scans the input object and the temporary document instead. Those hold the unconverted generator clones, whose matrices are dropped or which have no polygons at all.
* **Effect:** generators without a built cache (just created, or evaluated only in the temporary document) upload wrong geometry or nothing.
* **Fix:** use `result[0]` when the result is a non-empty list, keeping the bool and `BaseObject` cases for safety. Polygon parts are collected from the result hierarchy with accumulated matrices. The temporary document stays referenced until all data has been read, which replaces the `self._temp_cage_doc` lifetime hack.
* **Checks:** test `test_outbox.py::test_csto_result_list_is_used`; manual check `M-OUT-5`.

### F03 — Child and cache transforms dropped on the generator path

* **Where:** `core/handler.py:2151-2154`, together with `_collect_polygon_caches` (`:1875-1923`) and `_combine_polygon_sources` (`:1925-1963`).
* **What:** when `is_temp` is true, only `sds_obj.GetMg()` is applied. The local matrix of each SDS child is lost (`child.Ml`), and so are the local matrices inside a cache hierarchy. A single cloned cache object (no JOIN) also keeps only its own `Ml`.
* **Effect:** an offset or rotated Cube under an SDS uploads at the SDS origin. Clones of Symmetry or Array lose their placement.
* **Trigger:** SDS > Cube moved to (200, 0, 0). Plasticity receives the cube at the origin.
* **Fix:** matrices are accumulated explicitly while traversing:
  * a child contributes its parent's matrix times `child.GetMl()`;
  * a generator cache root contributes the generator's matrix times `cache.GetMl()`, and so on down the cache hierarchy;
  * a deform cache is taken in its host object's space (its points are the host's deformed points).
  
  Maxon documents that `GetMg()` is not valid on cache objects. That is why the chain is built by hand. The exported points are expressed relative to the **root null** (`~root.GetMg() * sds.GetMg() * part_matrix`), so a moved root and the unit scale are both inverted.
* **Basis:** the cache-root-relative-to-generator model is **(inferred)** from how generators return identity-matrix caches. It is checked in `M-OUT-3` and `M-OUT-4`.
* **Tests:** `test_outbox.py::test_offset_child_is_baked`, `::test_cache_hierarchy_matrices`, `::test_deform_cache_uses_host_space`, `::test_root_transform_and_unit_scale_inverted`.

### F04 — Export merges all SDS children; C4D subdivides only the first

* **Where:** `core/handler.py:1850-1873`.
* **What:** the C4D help says "The Subdivision Surface object will affect only its first child object", with that child's whole hierarchy. The code joins all children (generator path) or requires a single child (fast path).
* **Effect:** what Plasticity receives differs from what the user sees subdivided in C4D.
* **Fix:** export the first child and its hierarchy only, and warn when the SDS has more children ("ignored by Cinema 4D and by the upload").
* **Test:** `test_outbox.py::test_only_first_sds_child_is_exported`.

### F05 — N-gons exported as triangle/quad fragments

* **Where:** `core/handler.py:2169-2179`.
* **What:** C4D stores an N-gon as tris/quads plus hidden edges. The export sends each fragment as its own face, which changes the subdivision limit surface in Plasticity.
* **Fix:**
  * Group polygons into visible faces with `PolygonObject.GetPolygonTranslationMap()`. It returns `(face_count, polymap)`, mapping each polygon to its N-gon or face index.
  * Rebuild each N-gon's outline from its directed boundary edges (`geometry.ngon_outline`).
  * Reverse the winding back to Plasticity order. `GetNGonTranslationMap` is deliberately not used: its documented return layout is known to be wrong (forum topic 12819).
  * N-gons with holes or pinched vertices can't be represented as one face. They fall back to their fragments, with a warning.
* **Tests:** `test_geometry.py::test_ngon_outline_*`, `test_outbox.py::test_ngon_export_single_face`, `::test_ngon_with_hole_falls_back_to_fragments`, `::test_import_export_round_trip_tris`, `::test_import_export_round_trip_ngons`. Manual check `M-OUT-6`.

### F06 — Refresh stays disabled forever

* **Where:** `core/protocol.py:325-327`, `core/client.py:223-231`, `dialogs/main_dialog.py:526-562`.
* **What:**
  * A LIST response with code ≠ 200 makes the parser return `None`. No event is emitted, so the dialog's `_busy` flag is never cleared.
  * Any exception while parsing a LIST, REFACET or PUT_SOME response has the same effect.
  * A failed send only cleared `_busy` by accident, through the generic `STATUS_UPDATE` callback.
* **Trigger:** Plasticity has no open file (list fails), or a truncated message arrives.
* **Fix:**
  * The parser returns `{'error': code, ...}` for LIST failures, as it already does for REFACET.
  * The client emits `OPERATION_FAILED` for unreadable responses and failed sends.
  * Every handler checks for `error`. The dialog clears `_busy` and shows the error on every response, failure and disconnect.
  * As a last-resort guard, `_busy` expires after 120 s with a status message.
* **Tests:** `test_protocol.py::test_list_response_ok_and_error`, `test_client.py::test_unreadable_list_response_emits_operation_failed`, `test_dialog.py::test_busy_cleared_by_every_terminal_event`, `::test_busy_times_out`, `::test_refresh_send_failure_clears_busy_immediately`.

### F07 — A refacet error response creates an empty "Plasticity" root

* **Where:** `core/handler.py:563-577` together with `core/protocol.py:397-399`.
* **What:** the refacet error dict has no filename. `_on_refacet_response` calls `self._prepare(doc, '')` before checking anything, so a root null named `Plasticity` with an Inbox and Outbox is inserted into the scene.
* **Trigger:** refacet an object that Plasticity rejects, so the server returns a non-200 code.
* **Fix:** the handler checks `data.get('error')` first and reports the error.
* **Test:** `test_handler.py::test_refacet_error_does_not_touch_scene`.

### F08 — Events are dropped silently

* **Where:** `core/threading_bridge.py:47-48`, `:85-90`, `:107-116`.
* **What:** the queue has `maxsize=1000`, `put_nowait` failures are swallowed, and only 10 events are drained per 16 ms tick. A burst of Live Link transactions, or a closed dialog with Live Link on, silently loses updates and the scene desyncs.
* **Fix:**
  * The queue is an unbounded `deque` that never drops events.
  * It prints a warning when the backlog reaches 200, 400, 800 and so on.
  * `process_pending_events` drains until the queue is empty or a 50 ms budget is used (always at least one event), so it catches up without freezing the UI.
  * A re-entrancy guard (F27) keeps events in order.
* **Tests:** `test_threading_bridge.py` (ordering, a 5,000-event burst, time budget, callback exceptions, re-entrancy, 8 concurrent producers).

### F09 — The main thread blocks on sends and on disconnect

* **Where:** `core/client.py:259-275` (`_send_and_wait`: 5 s per request and 30 s for `put_some`) and `:115-143` (`disconnect`: 2 s wait plus a 3 s join).
* **What:** C4D's UI freezes for as long as the socket is slow or stuck.
* **Fix:**
  * Every public client call is fire-and-forget. The payload is encoded on the caller's thread and sent on the session's event loop. A failure arrives on the main thread as an `OPERATION_FAILED` event.
  * `disconnect()` returns immediately. A bounded join happens only at plugin shutdown (`C4DPL_ENDACTIVITY`).
* **Tests:** `test_client.py::test_calls_do_not_block_on_a_stalled_server`, `::test_disconnect_is_non_blocking`, `::test_send_when_not_connected_reports_operation_failed`, `::test_encode_error_is_reported_not_raised`.

---

## Medium

### F10 — Double-connect race; a dying connection clobbers a new one

* **Where:** `core/client.py:97-113`, `:153-170`.
* **What:** `connect()` only refuses while the bridge already says "connected". Clicking Connect twice while the first attempt is still connecting starts two threads and two event loops. Also, the `finally` block of a finishing thread sets `self._loop = None` and `bridge.connected = False`, even when a newer connection is already live.
* **Fix:** each connection is a `_Session` with its own loop and thread. A session may only change shared state, or push events, while it is the current session. `connect()` refuses while a session exists.
* **Tests:** `test_client.py::test_double_connect_is_refused`, `::test_reconnect_after_disconnect_is_not_clobbered`.

### F11 — Live Link undo pollution and double tessellation

* **Where:** `core/handler.py:447-561` (transaction/list), `:563-610` (refacet), `:727-769` (auto-refacet).
* **What:**
  * Every incoming message creates an undo step.
  * N-gon melts run after `EndUndo`.
  * With Auto-Refacet, each update rebuilds the object twice: tri geometry from the transaction, then the refacet response. Each rebuild is its own undo step.
* **Decision:**
  * Each incoming update stays **one** undo step, the same as the Blender bridge, which brackets every update with `undo_push`. An undo step is opened only if something actually changes, and all work, N-gon melts included, now happens inside it.
  * Remote changes are still recorded rather than made invisible to undo. Changing or deleting scene objects behind the undo system's back can leave the undo stack referring to states it never recorded, and Maxon has reported crashes from deletions made without `UNDOTYPE_DELETEOBJ`. The cost of this choice: Ctrl+Z after a Live Link update reverts the C4D copy to the previous Plasticity state, and the next update or a Refresh resyncs it. This is documented in the README.
  * Objects carrying an Auto-Refacet tag no longer get the intermediate tri rebuild. The transaction or list data is kept aside, and only the refacet response rebuilds the mesh, giving one tessellation and one undo step. If the refacet fails, the kept tri data is applied instead, so the object never stays stale. That covers an error response, a send failure or unreadable response (`OPERATION_FAILED`), a disconnect while the request is in flight, and an unusable refacet item. Refacet requests are also sent before the Outbox upload callback runs. Name, hierarchy and visibility are still applied immediately.
* **Tests:** `test_handler.py::test_one_undo_step_per_update`, `::test_no_empty_undo_steps`, `::test_ngon_melt_happens_inside_the_same_undo_step`, `::test_auto_refacet_skips_tri_rebuild`, `::test_auto_refacet_failure_applies_stashed_geometry`, `::test_auto_refacet_send_failure_applies_stash_immediately`. Manual checks `M-UNDO-*`, `M-AR-2`.

### F12 — Deformers ignored on the fast path

* **Where:** `core/handler.py:1855-1857`.
* **What:** a single polygon child is returned as is, without its `GetDeformCache()`. The cache path does respect deformers, so the result depends on how many children the SDS has.
* **Fix:** both cases now use one traversal, with the deform cache checked first. The order follows Maxon's `GetCache` recursion example.
* **Test:** `test_outbox.py::test_deform_cache_uses_host_space`. Manual check `M-OUT-7`.

### F13 — N-gon refacet with only tris/quads stores a wrong poly-face map

* **Where:** `core/handler.py:1231-1234` (called from `:597-601`).
* **What:** if a refacet in N-gon mode produces no polygon with more than 4 sides, nothing is melted, and the poly-face map is computed with tri-mode arithmetic (`loop = 3 * poly`). Quads span 4 loops, so polygons are assigned to the wrong CAD faces.
* **Effect:** Select/Store Faces and Edges pick the wrong regions.
* **Trigger:** refacet a box in N-gon mode, then Select Plasticity Face(s).
* **Fix:** use the per-polygon group computed by the N-gon builder (`poly_face_val`).
* **Test:** `test_handler.py::test_quad_only_ngon_refacet_face_map`.

### F14 — Normal tag paths

* **Where:** `core/handler.py:1283-1312`, `:1338-1371`.
* **What:**
  * `c4d.NormalTag.SetPolygon` does not exist in Python; the R26+ API is the static `NormalTag.Set(data, i, dict)`. The `AttributeError` is swallowed, so the low-level fallback always ran.
  * That fallback scales by 32767. Maxon (forum topic 11584) says the int16 values are scaled by **32000**, so 1.1.x normals were about 2.4 % too long.
  * It also assumed the memoryview is byte-formatted.
* **Fix:**
  * The low-level buffer is the primary (and fastest) path, with scale 32000.
  * The memoryview is cast to bytes and its length checked.
  * The high-level `NormalTag.Set` is the fallback.
* **Tests:** `test_geometry_equivalence.py::test_normal_packing_*` (layout identical to 1.1.6 apart from the scale), `test_handler.py::test_normal_tag_contents`, `test_geometry_equivalence.py::test_normal_scale_is_32000_per_maxon`. Manual check `M-IMP-3`.

### F15 — N-gon melt overwrites the polygon selection

* **Where:** `core/handler.py:1501-1504`.
* **What:** each melt batch replaces the object's polygon selection, and the last batch stays selected.
* **Fix:** clone the selection before melting (`BaseSelect.GetClone`) and restore it afterwards (`CopyTo`), clamped to the new polygon count.
* **Test:** `test_handler.py::test_melt_restores_polygon_selection`.

### F16 — README promises generator upload; other objects skipped silently

* **Where:** `README.md` (Features, Scene Hierarchy, Usage step 6), `core/handler.py:1770-1807`.
* **What:**
  * The README says Loft, Sweep, Lathe, Boole and other generators placed in the Outbox are uploaded. The code uploads only Subdivision Surfaces; generators count only as an SDS's control cage.
  * Anything that is not a Null, SDS or polygon object is skipped without a warning.
* **Fix:**
  * The docs now say what the code does: SDS objects whose first child, of any kind, forms the cage. Uploading non-SubD meshes as `KIND_MESH`, as Blender does, is listed as an open question.
  * Every skipped object gets a console warning and is counted in the status line.
* **Test:** `test_outbox.py::test_skipped_objects_are_reported`.

### F17 — `only_visible` ignores hidden parents and layers

* **Where:** `core/handler.py:1759-1766`.
* **What:** only the object's own editor dot is checked. An object set to undefined under a hidden parent still uploads. The condition is also convoluted.
* **Fix:** `effective_editor_visibility` walks up the hierarchy: the nearest explicit ON or OFF wins, and undefined defers to the parent, as the `GetEditorMode` docs describe. A layer with view disabled also hides the object.
* **Tests:** `test_outbox.py::test_only_visible_respects_hidden_parents`, `::test_only_visible_explicit_on_under_hidden_parent`, `test_geometry.py::test_effective_editor_visibility`.

### F18 — Duplicated Outbox objects share persistent IDs

* **Where:** `core/handler.py:1772-1775`, `:2133-2136`.
* **What:** Ctrl+drag or copy-paste of an uploaded SDS also copies its `BC_PLASTICITY_PNS_ID` and `BC_PLASTICITY_ID`. Both copies are then sent with the same `c4d_id` and `existing_stable_id`, so Plasticity updates one object twice and the copy never gets its own object. Null collection IDs have the same problem.
* **Fix:** within one gather, a repeated ID gets a fresh UUID and its stale Plasticity ID is cleared.
* **Test:** `test_outbox.py::test_duplicated_objects_get_fresh_ids`.

### F19 — Outbox ID filter mixes the item and group ID spaces

* **Where:** `core/handler.py:824-849`, `:910`, `:990`; `_uploaded_ids` at `:394` and `:697-705`.
* **What:** item IDs and group IDs overlap (Blender's handler notes this). An Inbox group whose ID equals an Outbox item's ID is skipped, and the reverse also happens.
* **Fix:** keep separate sets for item IDs and group IDs.
* **Test:** `test_handler.py::test_outbox_item_id_does_not_hide_inbox_group`.

### F20 — An uploaded object can be mirrored into the Inbox and never removed

* **Where:** `core/handler.py:482-502`.
* **What:** if a Live Link transaction carrying the just-uploaded object arrives before the PUT_SOME response that stamps its ID, the object is imported into the Inbox. Later refreshes skip it (its ID is an Outbox ID) and also never delete it, because stale deletion excludes Outbox IDs.
* **Basis:** server ordering is not specified, so this is defensive.
* **Fix:** Inbox copies of IDs returned by this session's PUT_SOME responses for the same file are removed during list and transaction processing. Other Outbox IDs are only skipped, as in 1.1.x (see F46).
* **Test:** `test_handler.py::test_inbox_mirror_of_uploaded_object_is_removed`.

### F21 — Out-of-range indices

* **Where:** `core/handler.py:1055-1060` (tri) and `:1095` (N-gon).
* **What:**
  * Tri mode passes any index straight into a `CPolygon`. Polygons with invalid point indices can crash C4D.
  * N-gon mode raises `IndexError` halfway through a transaction, or wraps negative int32 indices around silently.
* **Fix:** drop polygons that reference missing vertices, and report the count.
* **Tests:** `test_geometry.py::test_invalid_indices_are_dropped`, `::test_degenerate_ngon_faces_are_dropped`, `test_handler.py::test_invalid_indices_do_not_crash_and_are_dropped`.

### F22 — `on_list_complete` overwritten or left stale

* **Where:** `core/client.py:281-293`.
* **What:** an auto-refresh after NEW_VERSION/NEW_FILE calls `list_all()` without a callback, which sets the slot to `None` and silently cancels a pending Outbox upload. A failed list also leaves the callback armed, so it fires on some later list.
* **Fix:** only set the slot when a callback is passed, and clear it when a list fails.
* **Test:** `test_client.py::test_list_without_callback_keeps_pending_callback`, `test_handler.py::test_list_error_does_not_touch_scene_and_clears_callback`.

### F30 — Generic package names and `libs` inserted at `sys.path[0]`

* **Where:** `plasticity_c4d.pyp:20-25`.
* **What:** the plugin directory and `libs` are put at the front of the shared `sys.path`, and the code is imported as the top-level packages `core`, `dialogs` and `websockets`.
  * Another plugin with a `core` package, loaded first, makes this plugin import the wrong code.
  * A different `websockets` version may be picked up, either theirs or ours.
  * The order depends on plugin load order.
* **Fix:** load through `mxutils.LocalImportPath` where available, as Maxon SDK support recommended for this plugin; it also allows reloading modules during development. Cinema 4D 2023 has no `mxutils`, so it uses a small fallback with the same contract. The `sys.path` entries are not left behind. If another plugin has already loaded a different `websockets`, that copy is used with a console warning, which is what 1.1.x did silently. `PLASTICITY_BRIDGE_DEV=1` enables `autoReload` for development only.
* **Tests:** `test_plugin_entry.py`. Manual checks: the setup section and `M-DEV-1`.

### F45 — pre-1.1.0 scenes no longer migrated (regression in 1.1.6)

* **Where:** `core/handler.py:855-872` (`_is_managed_parent`) and `:800-818` (`_prepare` migration scan).
* **What:** `_prepare` picks up objects that sit directly under the root null (the pre-1.1.0 layout) "to be re-parented into the Inbox". In 1.1.6, `_is_managed_parent` was introduced to keep user re-parenting. It only treats the Inbox and Plasticity groups as managed, not the root null itself, so those objects are never moved.
* **Found by:** `test_handler.py::test_legacy_flat_scene_is_migrated_into_inbox` while writing the compatibility tests.
* **Fix:** the plugin's own root null of the same file counts as a managed parent.
* **Manual check:** `M-COMP-4`.

### F46 — a copy of an Inbox mesh used as a cage freezes the original

* **Where:** `core/handler.py:824-849` (`_get_outbox_plasticity_ids` collects `BC_PLASTICITY_ID` from **every** Outbox descendant).
* **What:** Ctrl+drag an imported mesh (ID 5) under a Subdivision Surface in the Outbox to use it as a cage. The copy keeps `BC_PLASTICITY_ID = 5`. From then on ID 5 counts as Outbox-owned, so the original Inbox object never updates again. Combined with the first version of the F20 fix, it was even deleted.
* **Found by:** the independent review of this branch, which reproduced it.
* **Fix:** only IDs the plugin stamped count:
  * the ID on a Subdivision Surface carrying `BC_PLASTICITY_PNS_ID`;
  * the group ID on a null carrying `BC_PLASTICITY_COLLECTION_ID`.
  
  Cages are not searched. Inbox mirrors are deleted only for IDs returned by this session's PUT_SOME (F20).
* **Tests:** `test_handler.py::test_copied_inbox_mesh_used_as_cage_does_not_hide_original`, `::test_stamped_sds_id_is_protected_but_not_deleted`.

### F47 — uploaded IDs leak across Plasticity files

* **Where:** `core/handler.py:394`, `:697-705`, `:833` (`_uploaded_ids` is a single set for the whole session).
* **What:** Plasticity IDs are small per-file integers. After uploading to file A (stable ID 7), switching Plasticity to file B hides B's object 7 for the rest of the session.
* **Fix:** uploaded IDs are kept per Plasticity file. The file is remembered when the upload is built, because the PUT_SOME response does not carry it.
* **Test:** `test_handler.py::test_uploaded_ids_are_tracked_per_plasticity_file`.

### F44 — deleting the Outbox duplicates the Inbox

* **Where:** `core/handler.py:1664-1686` and `:1638-1662`.
* **What:** when either null is missing, `_get_or_create_outbox` and `_get_or_create_inbox` call `_create_inbox_outbox`, which creates **both**. After the user deletes the Outbox, every scene then has a second, empty Inbox.
* **Fix:** create only the missing null. The Outbox is inserted as the first child and the Inbox as the last.
* **Tests:** `test_handler.py::test_deleting_outbox_does_not_duplicate_inbox`. Manual check `M-COMP-5`.

---

## Low

| ID | Where | Finding | Fix |
|---|---|---|---|
| F23 | `handler.py:875-885`, `:931` | Group renames, visibility and `BC_PLASTICITY_FLAGS` writes are made inside an undo block without `AddUndo`. | `AddUndo(UNDOTYPE_CHANGE_SMALL)` before each change, only when something actually changes. Test: `test_handler.py::test_visibility_and_rename_are_recorded_in_undo`. |
| F24 | `handler.py:2432-2566` | Select Faces, Select Edges and Select Sharp Edges change selections with no undo. | `UNDOTYPE_CHANGE_SELECTION` in one undo step per click. Test: `test_handler.py::test_selection_utilities_are_undoable`. |
| F25 | `README.md` "Managed Tags" | Says managed tags are "stripped and recreated" on every update. Only the normal tag is recreated; the face/edge selection tags are removed, because their indices would be stale. | Docs fixed. |
| F26 | `client.py:185-194` | `except Exception: continue` around `recv()` can spin forever on a repeating error. | Non-close errors end the session. Test: `test_client.py::test_receive_error_ends_session_instead_of_spinning`. |
| F27 | `threading_bridge.py:107-116`, `main_dialog.py:661-664` | `MessageDialog` inside a callback can let `Timer` re-enter `process_pending_events` and dispatch later events first. | Re-entrancy guard. Test: `test_threading_bridge.py::test_reentrant_processing_is_ignored`. |
| F28 | `pyp:66`, `client.py:80-83,182,321`, `handler.py:49,79,396,406,510,630,821,1619,1723`, `main_dialog.py:135,526` | "v2.1.0" labels describe features of plugin 1.1.x. | Removed or relabelled. |
| F29 | `main_dialog.py:871-874`, `pyp:103-116` | Closing the dialog disconnects (intended: the timer that drains events lives in the dialog). Problems: the disconnect blocked for up to 5 s; a connection still *connecting* was not shut down at exit; `DestroyWindow` also runs on layout switches. | Non-blocking disconnect; `C4DPL_ENDACTIVITY` calls `client.shutdown()` (bounded join, also covers connecting); behaviour documented in the README. Tests: `test_client.py::test_disconnect_while_connecting`, `::test_shutdown_joins_threads`, `test_dialog.py::test_destroy_window_disconnects_and_stops_timer`. Manual checks `M-LIFE-*`. |
| F31 | `handler.py:1227` | The Python docs list only `ResizeObject(pcnt[, vcnt])`; 1.1.6 passes `ncnt=0`. | Try 3 arguments, fall back to 2 on `TypeError`; manual check `M-IMP-5`. Test: `test_handler.py::test_resize_falls_back_when_ncnt_is_rejected`. |
| F32 | `handler.py:2228-2238` | Changing the unit scale only updates roots cached this session, so after a reconnect nothing changes until Refresh. | Find roots in the document. Test: `test_handler.py::test_unit_scale_updates_all_roots`. |
| F33 | `main_dialog.py:502-504`, `:614-648` | Every 16 ms tick re-applies ~25 enable states and the status text. | Apply only when the state changes. Test: `test_dialog.py::test_ui_state_is_only_reapplied_on_change`. |
| F34 | `protocol.py:122-169` | A position count that is not a multiple of 3, sizes that don't sum to the index count, or out-of-range indices are encoded anyway. | `ValueError` before sending; the client reports it. Tests: `test_protocol.py::test_put_some_rejects_inconsistent_mesh`, `test_client.py::test_encode_error_is_reported_not_raised`. |
| F35 | `main_dialog.py:540`, `handler.py:633-641` | `supported_messages` is only set when the handshake event is processed. A Refresh queued before it skips the upload. | The client records the capabilities as soon as the handshake is parsed. Test: `test_client.py::test_connect_handshake_list_subscribe_disconnect` (no scene handler attached). |
| F36 | `handler.py:943-947`, `:1223-1224` | An update that yields no polygons keeps the old mesh but overwrites groups and face IDs. | Leave the metadata alone when the geometry was not replaced. Test: `test_handler.py::test_empty_update_keeps_metadata`. |
| F37 | `handler.py:322-323`, `:345` | Corrupt poly-face map JSON raises inside utilities. | Tolerant decode. Tests: `test_handler.py::test_corrupt_poly_face_map_does_not_raise`, `test_geometry.py::test_poly_face_map_corrupt_data_is_none`. |
| F38 | `libs/__init__.py` | Mentions a compiled speedups module; only `speedups.c` is shipped, so the pure-Python path is always used. | Docstring fixed; `speedups.c` excluded from the release. |
| F39 | v1.1.6 release asset | Contains `.DS_Store` (twice), `speedups.c` and `py.typed`; no `LICENSE`, no websockets license. | `scripts/build_release.py` with an allowlist. Tests: `test_build_release.py`. |
| F41 | `handler.py:538-540`, `:1702-1707` | A transaction DELETE only removes items, so a deleted Plasticity group stays until the next Refresh. Blender behaves the same, and because item and group IDs overlap, a DELETE ID cannot be safely matched against groups. | **Deferred:** needs protocol knowledge of whether DELETE can carry group IDs. Open question. |
| F42 | `handler.py:1591-1605` | The unit scale is not stored in the scene. Reopening a scene saved with scale 2 and pressing Refresh resets the root to the dialog value (1.0). | **Deferred:** changing whether Refresh enforces the slider value is a UX decision. Open question. |
| F43 | `main_dialog.py:868-869` | `GeDialog` has no `RestoreLayout` hook (that belongs to `CommandData`, which already implements it), so this method is never called. | Removed. |

## Info

* **F40, protocol cross-check:** every message type, field order, alignment and endianness matches the Blender addon. Two differences are Blender bugs, and the C4D bridge is the correct one:
  * Blender reads a DELETE item's count from offset 0, which is the type field. The IDs start at offset 8, which fits the `[type][count][ids]` layout the C4D bridge reads.
  * Blender prefixes filenames in `subscribe_some`/`refacet_some` with `len(str)` instead of the UTF-8 byte length. For ASCII the two are identical, and the C4D bridge sends the byte length, which is what PUT_SOME uses in Blender too.
  
  The wire format is unchanged; golden-byte tests pin it.

---

## Performance

Measured with `python tests/perf/bench.py` on synthetic Plasticity-style meshes (`tests/meshgen.py`), comparing the frozen 1.1.6 code in `tests/reference/` with the new code. Cinema 4D API calls are **not** included: in C4D, `SetAllPoints` and `GetAllPoints` replace per-element calls, but their speed can only be measured inside C4D.

| # | Hot spot (1.1.6) | Change | 100k tris | 1M tris |
|---|---|---|---|---|
| P01 | Decoding builds Python lists for every float and int (`protocol.py:202-226`) | `array.frombytes` (one memcpy) with a byteswap guard | 7 ms → 0.05 ms (138×) | 57 ms → 0.7 ms (87×) |
| P02 | `encode_put_some` uses `bytes +=` on the growing message; quadratic in the number of objects (`protocol.py:133-169`) | Parts list plus one `b"".join`; `array.tobytes` for bulk data | 1,998 objects: 766 ms → 7 ms (109×) | 19,993 objects: **65.8 s → 134 ms (489×)** |
| P02b | Same, one large object | Same, plus consistency validation | 4 ms → 3 ms (1.1×) | 38 ms → 32 ms (1.2×) |
| P03 | Per-element `SetPoint` (`handler.py:1209-1212`) | `SetAllPoints` (Ferdinand: "much faster"). Python has no bulk polygon setter, so `SetPolygon` stays a loop | Only measurable in C4D | Only measurable in C4D |
| P04 | Normal tag data built per corner: 12 clamp/pack calls per polygon (`handler.py:1293-1312`) | Quantise once per vertex, then join 6-byte blobs per corner | 154 ms → 28 ms (5.4×) | 1,586 ms → 309 ms (5.1×) |
| P05 | Welding: three `round()` calls per vertex (`handler.py:1085-1093`) | Exact-tuple lookup first; `round()` only once per distinct position (same result) | N-gon build 15k polys: 116 ms → 79 ms (1.5×) | 155k polys: 1,384 ms → 855 ms (1.6×) |
| P06 | Poly-face map stored as a JSON list (one int per polygon), parsed by every utility | Run-length JSON (`{"v":1,"rle":[...]}`); 1.1.x maps still read | 386 KB → 604 B; load 4 ms → 1 ms | **3.8 MB → 589 B per object** in the .c4d; load 42 ms → 13 ms |
| P07 | One full-document walk per PUT_SOME result (`handler.py:670, 683, 709-721`) | One indexing walk per response | O(results × objects) → O(objects) | Same |
| P08 | `_insert_last_child` walks all siblings for each insertion, so building a large Inbox is quadratic (`handler.py:1710-1720`) | `InsertUnderLast` (C++) | Only measurable in C4D | Only measurable in C4D |
| P09 | Tri geometry: per-index loops plus a bisect per polygon for the face map | Slicing with `zip`, run-filled face map | 67 ms → 51 ms (1.3×) | 804 ms → 571 ms (1.4×) |
| P10 | Select Sharp Edges: a `c4d.Vector` normal for every polygon, through per-call API access | Bulk `GetAllPoints`/`GetAllPolygons`; normals only for candidate edges | 252 ms → 86 ms (2.9×) | 2,806 ms → 1,032 ms (2.7×) |
| P11 | Dialog `Timer` re-applies ~25 gadget states every 16 ms (F33) | Only when the state changes | — | — |

**Method:**
* Run `python tests/perf/bench.py --sizes 100000 1000000`. It reports the median of 3 runs at 100k and a single run at 1M, on CPython 3.11.15, macOS, Apple silicon.
* Every pair of outputs is asserted identical before it is timed. The normal data is compared at 1.1.6's scale.
* The tri-geometry numbers include building the stub's `c4d.Vector`/`CPolygon` objects on both sides. Those are slower than C4D's C++ types, so real-world gains on that row are larger than shown.

**Remaining cost:** in 1M-triangle imports, Python-side polygon creation (`SetPolygon` per polygon) and N-gon melting now dominate. Both need C4D to measure.

---

## Sources

* `SendModelingCommand`, return types, the doc requirement and "copy as result": https://developers.maxon.net/docs/py/2025_0_0/modules/c4d.utils/index.html
* `MCOMMAND_JOIN` ("Passing multiple objects into SMC will not join them."): https://developers.maxon.net/docs/py/2026_0_0/consts/MCOMMAND.html and https://developers.maxon.net/docs/cpp/2026_0_0/group___m_c_o_m_m_a_n_d.html
* JOIN result transform: https://developers.maxon.net/forum/topic/11772, https://developers.maxon.net/forum/topic/14380
* SMC undo flags: https://developers.maxon.net/docs/cpp/2026_0_0/group___m_o_d_e_l_i_n_g_c_o_m_m_a_n_d_f_l_a_g_s.html and https://developers.maxon.net/forum/topic/11611
* Caches, `GetMg()` not valid on cache objects, and the `GetCache` recursion example: https://developers.maxon.net/docs/py/2025_0_0/modules/c4d/C4DAtom/GeListNode/BaseList2D/BaseObject/index.html and https://developers.maxon.net/docs/cpp/2026_0_0/page_manual_baseobject.html
* Deform cache drawn in the host's space: https://developers.maxon.net/forum/topic/14462
* SDS affects only its first child: https://help.maxon.net/c4d/2026/en-us/Content/html/5166.html
* `GetPolygonTranslationMap`, `GetNGonTranslationMap` and `ResizeObject(pcnt[, vcnt])`: https://developers.maxon.net/docs/py/2024_0_0/modules/c4d/C4DAtom/GeListNode/BaseList2D/BaseObject/PointObject/PolygonObject/index.html. The `GetNGonTranslationMap` docs error is discussed at https://developers.maxon.net/forum/topic/12819.
* `GetAllPoints`/`SetAllPoints`: https://developers.maxon.net/docs/py/2026_0_0/modules/c4d/C4DAtom/GeListNode/BaseList2D/BaseObject/PointObject/index.html and https://developers.maxon.net/forum/topic/13327
* NormalTag API (R26+ `Set`/`Get`, `GetLowlevelDataAddressW`): https://developers.maxon.net/docs/py/2026_0_0/modules/c4d/C4DAtom/GeListNode/BaseList2D/BaseTag/VariableTag/NormalTag/index.html. The 12 × int16 layout and the 32000 scale: https://developers.maxon.net/forum/topic/11584
* Editor visibility (MODE_ON overrides the parent; MODE_UNDEF inherits): `GetEditorMode` on the BaseObject page above
* `BaseSelect.CopyTo`/`GetClone`: https://developers.maxon.net/docs/py/2026_0_0/modules/c4d/BaseSelect/index.html
* `Matrix` operators: https://developers.maxon.net/docs/py/2026_0_0/modules/c4d/Matrix/index.html
* `C4DPL_ENDACTIVITY` ("close dialogs, end threads"): https://developers.maxon.net/docs/py/2026_0_0/misc/pluginstructure.html
* `DestroyWindow` also runs on layout switches: https://developers.maxon.net/docs/py/2026_0_0/modules/c4d.gui/GeDialog/index.html
* Deletion without `UNDOTYPE_DELETEOBJ` crashing (bound joints, 2023.1): https://developers.maxon.net/forum/topic/14431
* Embedded Python versions (2023.0/2023.1 = 3.9, 2023.2 = 3.10, 2024–2026 = 3.11): https://developers.maxon.net/docs/py/2023_1/index.html, https://developers.maxon.net/docs/py/2023_2/index.html and https://developers.maxon.net/docs/py/2024_0_0/index.html
