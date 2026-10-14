# Manual test checklist (inside Cinema 4D)

The automated suite (`python -m pytest`) covers the protocol, the threading layer, the client against a real WebSocket server, all c4d-free geometry, handler logic against a `c4d` stub, and plugin loading in a shared interpreter. It **cannot** run Cinema 4D itself, so everything below has to be checked by hand before a release.

The IDs (`F..`) refer to [AUDIT.md](../AUDIT.md). Items marked **(inferred)** rely on Cinema 4D behaviour that could only be inferred from the docs, so check them carefully. `tests/test_docs.py` fails if `AUDIT.md` cites a check ID that is missing here.

## Setup

- [ ] **Versions.** Run the checks on at least Cinema 4D **2023.x** (Python 3.9/3.10) and **2026.4 or newer** (Python 3.11, built-in MCP server).
- [ ] **Clean install.** Build with `python scripts/build_release.py`. **Delete any old `PlasticityBridge` folder**, then unzip `dist/PlasticityBridge_v<version>.zip` into the C4D `plugins` folder. It contains exactly `PlasticityBridge/plasticity_c4d.pyp`, `plasticity_bridge/`, `res/`, `LICENSE` and `readme.txt`.
- [ ] **Console.** Open *Extensions → Console* and keep it visible. Any traceback is a failure.
- [ ] **Load message.** Plasticity is running with a file open. Start C4D. The console shows `[Plasticity Bridge] <version> registered (ID: 1066929)` and `Auto-Refacet tag registered (ID: 1068209)`.
- [ ] **No leaked modules or paths (F30).** In the Python console, run each line below.

  | Expression | Expected result |
  |---|---|
  | `import sys; [p for p in sys.path if 'PlasticityBridge' in p]` | `[]` |
  | `[n for n in ('core', 'dialogs', 'libs') if n in sys.modules]` | `[]` |
  | `'PlasticityBridge' in getattr(sys.modules.get('websockets'), '__file__', '')` | `False` (`websockets` may be loaded by Cinema 4D or another plugin, but never from this plugin's folder) |
  | `sys.modules['plasticity_bridge'].__file__` | `<plugins>/PlasticityBridge/plasticity_bridge/__init__.py` |

## 1. Connect and refresh

| # | Steps | Expected |
|---|---|---|
| M-CON-1 | *Extensions → Plasticity Bridge*. Leave the server at `localhost:8980` and click **Connect**. | The status reads "Connected", then "Handshake OK". The controls become enabled. |
| M-CON-2 | Double-click **Connect** quickly while Plasticity is **not** running. | One "Connection failed" dialog. No second connection. The UI stays responsive (F10). |
| M-CON-3 | Click **Refresh**. | A `Plasticity: <file>` root with **Outbox** first and **Inbox** second. Objects and groups mirror Plasticity. The status reads "Refreshed <file>". |
| M-CON-4 | Close the file in Plasticity (so the list fails), then click **Refresh**. | The status shows an error. **Refresh is enabled again** (F06). |
| M-CON-5 | Click **Refresh** five times quickly. | One import. The button stays disabled until the response arrives. No duplicate objects. |
| M-CON-6 | Toggle **Only Visible**, hide an object in Plasticity, then Refresh. | The hidden object is not imported or is removed. |

## 2. Live Link

| # | Steps | Expected |
|---|---|---|
| M-LL-1 | Enable **Live Link**. Create a box in Plasticity. | It appears in the Inbox within a frame or two. |
| M-LL-2 | Edit the box (move a face, fillet an edge). | The same C4D object updates in place. Its materials, user tags and animation stay. |
| M-LL-3 | Delete the box in Plasticity. | The C4D object is removed. |
| M-LL-4 | Put a box in a group in Plasticity, then rename and hide the group. | The null mirrors it: name, parent and visibility. |
| M-LL-5 | Make 50 quick edits in Plasticity (drag a slider). | C4D follows and ends in the final state. No "events waiting" warning unless C4D is truly blocked (F08). |
| M-LL-6 | Open another file and use *File → New* in Plasticity. | C4D re-imports automatically (NEW_FILE / NEW_VERSION). |
| M-LL-7 | Inside the Inbox, move an object under your own Subdivision Surface, then edit it in Plasticity. | It stays under your SDS and keeps updating (1.1.6 behaviour). |
| M-LL-8 | Override an object's editor visibility in C4D, then Refresh. | Your override survives until the visibility changes in Plasticity. |

## 3. Undo (F11, F23, F24)

| # | Steps | Expected |
|---|---|---|
| M-UNDO-1 | After a Live Link edit, press **Ctrl+Z** once. | The C4D copy goes back to the previous Plasticity state in **one** step, N-gon melt included. No partial or "half-melted" state. |
| M-UNDO-2 | Press **Ctrl+Shift+Z** (redo). | The updated geometry comes back. |
| M-UNDO-3 | Undo, then edit the object in Plasticity again. | C4D resyncs to the new Plasticity state. |
| M-UNDO-4 | Refresh with nothing changed in Plasticity. | **No** new undo entry appears in *Edit → Undo*. |
| M-UNDO-5 | Live Link deletes an object; press Ctrl+Z. | The object comes back. No crash. Scrubbing undo and redo 20× back and forth stays stable. |
| M-UNDO-6 | Run Select Plasticity Faces, Select Plasticity Edges and Select Sharp Edges, then Ctrl+Z each one. | Each selection change undoes in one step. |
| M-UNDO-7 | Drag the Unit Scale slider, then Ctrl+Z. | The root scale reverts. |

## 4. Refacet, N-gons and Auto-Refacet

| # | Steps | Expected |
|---|---|---|
| M-IMP-1 | Select an imported object. Choose **Tris**, then **Refacet Selected**. | Pure triangles. Shading is smooth where Plasticity's is. |
| M-IMP-2 | Choose **Ngons**, then **Refacet Selected**. | Real N-gons: no inner edges in polygon mode. Refacet again with the same settings: still N-gons (1.1.6 fix). |
| M-IMP-3 | Inspect shading on a curved face and on a hard edge in both modes. Compare with Plasticity. | It matches Plasticity. The `__plasticity_normals__` tag is present. No dark or blown-out shading from wrongly scaled normals (F14: scale 32000). **(inferred)** |
| M-IMP-4 | Before the N-gon refacet, select some polygons in polygon mode. | After the refacet, the selection is not replaced by random melted faces (F15). |
| M-IMP-5 | Refacet several times in a row, alternating Tris and Ngons. | No errors. A console `TypeError` about `ResizeObject` would mean the 2-argument fallback ran (that's fine, but report it) (F31). |
| M-IMP-6 | In **Ngons** mode, refacet a box (only quads). Then run **Select Plasticity Face(s)** on one polygon. | Exactly that CAD face is selected (F13). |
| M-AR-1 | Check **Auto-Refacet**, then **Refacet Selected**. | A "Plasticity Auto-Refacet" tag is added with the dialog's settings. |
| M-AR-2 | Edit the tagged object in Plasticity with Live Link on. | The mesh updates **once**, straight to the refaceted result: no flash of triangles. **One** undo step (F11). |
| M-AR-3 | Change the tag's settings in the Attribute Manager, then Refresh. | The new settings are used. |
| M-AR-4 | Disconnect while a refacet is pending, edit the object in Plasticity, reconnect, then Refresh. | The object is refaceted; it is not left as raw triangles or stale (F11). |

## 5. Outbox upload (PUT_SOME)

Each of these needs a Plasticity build whose handshake lists PUT_SOME.

| # | Steps | Expected |
|---|---|---|
| M-OUT-1 | **Single cage.** Outbox > Subdivision Surface > editable Cube, then Refresh. | Plasticity receives one SubD box at the same place and size. |
| M-OUT-2 | **Multi-part cage.** Outbox > SDS > Null > two Cubes, one moved 300 cm on X. | Plasticity receives **both** boxes, 3 m apart (F01). |
| M-OUT-3 | **Offset child.** Outbox > SDS > *parametric* Cube moved to (200, 0, 0) and rotated 45°. | The upload is 2 m off-centre and rotated, the same as in C4D (F03). **(inferred: cache matrices)** |
| M-OUT-4 | **Generators.** Separate SDS cages each containing a Symmetry, a Boole, a Cloner, a Loft (two splines), a Sweep, and a polyspline-driven Extrude, with the generators moved and rotated. | Each arrives in Plasticity looking the same as the subdivided result in C4D (F03). **(inferred: generator cache root is relative to the generator)** |
| M-OUT-5 | **No cache yet.** Create a fresh parametric Cube under an SDS in the Outbox and Refresh **immediately**, before the viewport redraws. | It is uploaded via the temporary-document fallback (F02). |
| M-OUT-6 | **N-gons.** Outbox > SDS > polygon object with a pentagon and a hexagon cap (for example a cylinder with N-gon caps). | Plasticity receives the caps as single faces, not triangle fans (F05). |
| M-OUT-7 | **Deformers.** Outbox > SDS > editable Cube with a **Bend** deformer child. Also try a parametric Cube with a Bend child. | The bent shape is uploaded in both cases (F12). **(inferred: deform cache space)** |
| M-OUT-8 | **Several SDS children.** SDS > Cube A, Cube B. | Only Cube A is uploaded, which is what C4D subdivides. The console prints a note (F04). |
| M-OUT-9 | **Duplicate.** After a successful upload, Ctrl+drag the SDS to copy it, then Refresh. | Two objects in Plasticity; the copy does not overwrite the original (F18). |
| M-OUT-10 | **Skipped objects.** A Loft or a polygon object placed directly in the Outbox (not under an SDS). | It is not uploaded. A console warning names it, and the status says "N Outbox object(s) skipped" (F16). |
| M-OUT-11 | **Only Visible.** Hide the Outbox group containing an SDS, or put it on a layer with the view off, then Refresh with Only Visible checked. | That SDS is not uploaded. An SDS whose own dot is green (ON) inside a hidden group **is** uploaded (F17). |
| M-OUT-12 | **Live Link.** With Live Link on, upload a new cage. | It does not also appear (or stay) as a copy in the Inbox (F20). |
| M-OUT-13 | **User data.** Add boolean User Data `pns_rounded_corners` / `pns_merge_patches` / `pns_interpolate_boundary` to an SDS. | Plasticity's SubD options follow them. |
| M-OUT-14 | **Copied Inbox mesh as cage.** Ctrl+drag an imported Inbox mesh under an SDS in the Outbox, then edit the original in Plasticity with Live Link on. Switch Plasticity to another file and Refresh. | The Inbox original keeps updating and is never deleted (F46). The other file's objects all import, including ones whose IDs match uploaded objects (F47). |

## 6. Unit scale

| # | Steps | Expected |
|---|---|---|
| M-US-1 | Set Unit Scale to 2, then Refresh. | The root null is scaled ×2. Geometry is not rebaked. |
| M-US-2 | With scale 2, upload an Outbox cage of 100 cm. | Plasticity receives a **1 m** box: the scale is inverted (F03). |
| M-US-3 | Move the root null by hand, then upload. | Plasticity positions don't change: the export is root-relative. |
| M-US-4 | Disconnect, reconnect, then change Unit Scale **before** refreshing. | Every Plasticity root in the scene updates (F32). |

## 7. Utilities

| # | Steps | Expected |
|---|---|---|
| M-UT-1 | **Store: All Plasticity Faces.** | One `Plasticity Face <n>` polygon selection tag per CAD face, each matching its face. |
| M-UT-2 | **Store: All Plasticity Edges.** | One `__plasticity_edges__` edge selection tag with every CAD face border. |
| M-UT-3 | Polygon mode: select one polygon, then **Select: Plasticity Face(s)**. | The whole CAD face is selected. |
| M-UT-4 | **Select: Plasticity Edges.** | The outline of the selected CAD faces is selected; the tool switches to edge mode. |
| M-UT-5 | **Select Sharp Edges** at angle 0, then at 30°. | All CAD edges at 0°; only creases sharper than 30° at 30°. |
| M-UT-6 | Repeat M-UT-1 to M-UT-5 on an **N-gon**-refaceted object. | Correct results. |
| M-UT-7 | Live Link update on an object with stored face/edge tags. | The stale managed tags are removed; your own tags remain. |

## 8. Scene compatibility

| # | Steps | Expected |
|---|---|---|
| M-COMP-1 | Open a scene **saved with 1.1.6** (Inbox/Outbox, auto-refacet tags, stored face tags). | It opens with no errors. Tags show as "Plasticity Auto-Refacet" with their settings. |
| M-COMP-2 | In that scene, run every utility **before** refreshing. | They work: 1.1.x poly-face maps are still read. |
| M-COMP-3 | Connect and Refresh. | Objects update **in place**: no duplicates, materials kept. |
| M-COMP-4 | Open a scene from **before 1.1.0** (objects directly under the root), then Refresh. | Objects move into the Inbox (F45). |
| M-COMP-5 | Delete the Outbox null, then Refresh. | Exactly one new Outbox and still one Inbox (F44). |
| M-COMP-6 | Save the scene with 1.2, reopen it, and run the utilities. | They work (run-length poly-face maps). |
| M-COMP-7 | **Downgrade check (informational).** Open a 1.2-saved scene in 1.1.6 and run a utility before refreshing. | It fails until the next Refresh rewrites the maps. Expected; mention it in the release notes. |

## 9. Dialog lifecycle and shutdown (F29)

| # | Steps | Expected |
|---|---|---|
| M-LIFE-1 | While connected with Live Link on, close the dialog. | It disconnects at once; the UI doesn't freeze. Reopen the dialog and **Connect** again: it works first time. |
| M-LIFE-2 | Dock the dialog in a layout, switch layouts, then switch back. | The dialog shows "Disconnected" (closing disconnects by design). Reconnecting works. |
| M-LIFE-3 | **Quit C4D while connected** with Live Link on. | It quits promptly: no hang, no crash report. |
| M-LIFE-4 | Click Connect with Plasticity **not** running, then quit C4D within 2 s, while the attempt is still pending. | It quits promptly. |
| M-LIFE-5 | Pull the network (or quit Plasticity) while connected. | The status reads "Disconnected from server". Live Link unchecks. Reconnecting works. |
| M-LIFE-6 | Mid-upload of a large Outbox (100k+ polygons), click Disconnect. | The UI stays responsive (F09). |

## 10. Development reload

| # | Steps | Expected |
|---|---|---|
| M-DEV-1 | Start C4D with the environment variable `PLASTICITY_BRIDGE_DEV=1`. Close the bridge dialog, edit a module under `plasticity_bridge/` (e.g. a status text in `core/handler.py`), then run *Extensions → Reload Python Plugins*. Re-run the setup section's leak checks afterwards. | The change is picked up without restarting C4D (the package is re-imported from disk). There is no "installed twice" error, and the leak checks still pass. Without the variable, a reload keeps the already-loaded code. |

## 11. Coexistence with other plugins (F30)

| # | Steps | Expected |
|---|---|---|
| M-MCP-1 | Cinema 4D 2026.4+, MCP enabled, Plasticity Bridge installed → restart C4D. | The MCP page appears in Preferences, a client connects, and "create a cube" works. The console shows no `ImportError: cannot import name 'ServerProtocol'`. The bridge still connects to Plasticity (M-CON-1) and Refresh works (M-CON-3). |
