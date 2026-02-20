"""
Main dialog for Plasticity Bridge plugin.

Changes from original:
  Bug 1 fixed : RestoreLayout now calls self.Restore() not self.Open()
  Bug 4 fixed : _update_ui_state() called on EVERY timer tick, not only when
                events arrive — status messages from StatusReporter now show
                immediately.
  Feature 7   : Unit Scale slider added; drives SceneHandler.update_unit_scale()
                which applies scale to root null transforms without re-importing.
  Feature 8   : NEW_VERSION callback updates status label with "refresh available"
                message; NEW_FILE updates filename label automatically.
  Utilities   : All utility buttons removed from UI; a placeholder label
                informs the user they are coming soon.
"""

import c4d
import math
from c4d import gui
from modules.threading_bridge import EventType, BridgeEvent
from modules.protocol import FacetShapeType

PLUGIN_ID = 1066929


class IDS:
    # Groups
    GRP_MAIN            = 1000
    GRP_CONNECTION      = 1001
    GRP_STATUS          = 1002
    GRP_REFRESH         = 1003
    GRP_LIVELINK        = 1004
    GRP_REFACET         = 1005
    GRP_REFACET_INNER   = 1006
    GRP_REFACET_ADVANCED= 1007
    GRP_UNITS           = 1008
    GRP_UTILITIES       = 1009

    # Connection
    LBL_SERVER          = 1100
    EDT_SERVER          = 1101
    BTN_CONNECT         = 1102
    BTN_DISCONNECT      = 1103

    # Status
    LBL_STATUS          = 1200
    LBL_FILENAME        = 1201

    # Refresh
    CHK_ONLY_VISIBLE    = 1300
    BTN_REFRESH         = 1301

    # Live link
    CHK_LIVELINK        = 1400

    # Refacet
    GRP_REFACET_RADIOS  = 1500
    RDO_TRI             = 1501
    RDO_NGON            = 1502
    SLD_TOLERANCE       = 1503
    SLD_ANGLE           = 1504
    CHK_ADVANCED        = 1505
    SLD_MIN_WIDTH       = 1600
    SLD_MAX_WIDTH       = 1601
    SLD_CURVE_CHORD_TOL = 1602
    SLD_CURVE_CHORD_ANG = 1603
    SLD_SURF_PLANE_TOL  = 1604
    SLD_SURF_ANGLE_TOL  = 1605
    BTN_REFACET         = 1700

    # Units
    SLD_UNIT_SCALE      = 1800

    # Utilities placeholder
    LBL_UTILITIES_SOON  = 1900


class PlasticityDialog(gui.GeDialog):
    # ~60 fps — keeps the status label responsive while live-link is active
    TIMER_INTERVAL = 16

    def __init__(self, client, handler, bridge):
        super().__init__()
        self.client   = client
        self.handler  = handler
        self.bridge   = bridge

        # Dialog state
        self._server            = "localhost:8980"
        self._only_visible      = False
        self._tri_mode          = True
        self._show_advanced     = False
        self._tolerance         = 0.01
        self._angle             = 0.45
        self._min_width         = 0.0
        self._max_width         = 0.0
        self._curve_chord_tol   = 0.01
        self._curve_chord_angle = 0.35
        self._surface_plane_tol = 0.01
        self._surface_angle_tol = 0.35
        self._unit_scale        = 1.0
        self._busy              = False

        # Register bridge callbacks for connection-state changes
        bridge.register_callback(EventType.CONNECTED,        self._on_connected)
        bridge.register_callback(EventType.DISCONNECTED,     self._on_disconnected)
        bridge.register_callback(EventType.CONNECTION_ERROR, self._on_connection_error)
        bridge.register_callback(EventType.NEW_VERSION,      self._on_new_version)
        bridge.register_callback(EventType.NEW_FILE,         self._on_new_file)

    # =========================================================================
    # Layout
    # =========================================================================

    def CreateLayout(self):
        self.SetTitle("Plasticity Bridge")

        if self.GroupBegin(IDS.GRP_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0, ""):
            self.GroupBorderSpace(8, 8, 8, 8)

            # ── Connection ──────────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_CONNECTION, c4d.BFH_SCALEFIT, 2, 0, ""):
                self.GroupBorderNoTitle(c4d.BORDER_GROUP_IN)
                self.GroupBorderSpace(4, 4, 4, 4)
                self.AddStaticText(IDS.LBL_SERVER, c4d.BFH_LEFT, name="Server:")
                self.AddEditText(IDS.EDT_SERVER, c4d.BFH_SCALEFIT, initw=200)
            self.GroupEnd()

            if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                self.AddButton(IDS.BTN_CONNECT,    c4d.BFH_SCALEFIT, name="Connect")
                self.AddButton(IDS.BTN_DISCONNECT, c4d.BFH_SCALEFIT, name="Disconnect")
            self.GroupEnd()

            self.AddSeparatorH(c4d.BFH_SCALEFIT)

            # ── Status ───────────────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_STATUS, c4d.BFH_SCALEFIT, 1, 0, ""):
                self.AddStaticText(IDS.LBL_STATUS,   c4d.BFH_SCALEFIT, name="Status: Disconnected")
                self.AddStaticText(IDS.LBL_FILENAME, c4d.BFH_SCALEFIT, name="File: -")
            self.GroupEnd()

            self.AddSeparatorH(c4d.BFH_SCALEFIT)

            # ── Refresh ──────────────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_REFRESH, c4d.BFH_SCALEFIT, 2, 0, ""):
                self.AddCheckbox(IDS.CHK_ONLY_VISIBLE, c4d.BFH_LEFT,
                                 initw=0, inith=0, name="Only Visible")
                self.AddButton(IDS.BTN_REFRESH, c4d.BFH_SCALEFIT, name="Refresh")
            self.GroupEnd()

            self.AddSeparatorH(c4d.BFH_SCALEFIT)

            # ── Live Link ────────────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_LIVELINK, c4d.BFH_SCALEFIT, 1, 0, ""):
                self.AddCheckbox(IDS.CHK_LIVELINK, c4d.BFH_LEFT,
                                 initw=0, inith=0, name="Live Link")
            self.GroupEnd()

            self.AddSeparatorH(c4d.BFH_SCALEFIT)

            # ── Unit Scale ───────────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_UNITS, c4d.BFH_SCALEFIT, 2, 0, ""):
                self.AddStaticText(0, c4d.BFH_LEFT, name="Unit Scale:")
                self.AddEditSlider(IDS.SLD_UNIT_SCALE, c4d.BFH_SCALEFIT)
            self.GroupEnd()

            self.AddSeparatorH(c4d.BFH_SCALEFIT)

            # ── Refacet Settings ─────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_REFACET, c4d.BFH_SCALEFIT, 1, 0, "Refacet Settings"):
                self.GroupBorder(c4d.BORDER_GROUP_IN)
                self.GroupBorderSpace(4, 4, 4, 4)

                # Tri / Ngon radio buttons
                self.AddRadioGroup(IDS.GRP_REFACET_RADIOS, c4d.BFH_LEFT, 2, 1)
                self.AddChild(IDS.GRP_REFACET_RADIOS, IDS.RDO_TRI,  "Tri")
                self.AddChild(IDS.GRP_REFACET_RADIOS, IDS.RDO_NGON, "Ngon")

                # Simple tolerance / angle
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                    self.AddStaticText(0, c4d.BFH_LEFT, name="Tolerance:")
                    self.AddEditSlider(IDS.SLD_TOLERANCE, c4d.BFH_SCALEFIT)
                self.GroupEnd()

                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                    self.AddStaticText(0, c4d.BFH_LEFT, name="Angle:")
                    self.AddEditSlider(IDS.SLD_ANGLE, c4d.BFH_SCALEFIT)
                self.GroupEnd()

                # Advanced toggle
                self.AddCheckbox(IDS.CHK_ADVANCED, c4d.BFH_LEFT,
                                 initw=0, inith=0, name="Advanced Settings")

                # Advanced group (hidden by default)
                if self.GroupBegin(IDS.GRP_REFACET_ADVANCED, c4d.BFH_SCALEFIT, 1, 0, ""):
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Min Width:")
                        self.AddEditSlider(IDS.SLD_MIN_WIDTH, c4d.BFH_SCALEFIT)
                    self.GroupEnd()
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Max Width:")
                        self.AddEditSlider(IDS.SLD_MAX_WIDTH, c4d.BFH_SCALEFIT)
                    self.GroupEnd()
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Edge Chord Tol:")
                        self.AddEditSlider(IDS.SLD_CURVE_CHORD_TOL, c4d.BFH_SCALEFIT)
                    self.GroupEnd()
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Edge Chord Angle:")
                        self.AddEditSlider(IDS.SLD_CURVE_CHORD_ANG, c4d.BFH_SCALEFIT)
                    self.GroupEnd()
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Face Plane Tol:")
                        self.AddEditSlider(IDS.SLD_SURF_PLANE_TOL, c4d.BFH_SCALEFIT)
                    self.GroupEnd()
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, ""):
                        self.AddStaticText(0, c4d.BFH_LEFT, name="Face Angle Tol:")
                        self.AddEditSlider(IDS.SLD_SURF_ANGLE_TOL, c4d.BFH_SCALEFIT)
                    self.GroupEnd()
                self.GroupEnd()  # GRP_REFACET_ADVANCED

                self.AddButton(IDS.BTN_REFACET, c4d.BFH_SCALEFIT, name="Refacet Selected")
            self.GroupEnd()  # GRP_REFACET

            self.AddSeparatorH(c4d.BFH_SCALEFIT)

            # ── Utilities (coming soon placeholder) ──────────────────────────
            if self.GroupBegin(IDS.GRP_UTILITIES, c4d.BFH_SCALEFIT, 1, 0, "Utilities"):
                self.GroupBorder(c4d.BORDER_GROUP_IN)
                self.GroupBorderSpace(4, 6, 4, 6)
                self.AddStaticText(
                    IDS.LBL_UTILITIES_SOON,
                    c4d.BFH_CENTER,
                    name="Select Face / Mark Edges / Paint Faces — coming soon"
                )
            self.GroupEnd()

        self.GroupEnd()  # GRP_MAIN
        return True

    # =========================================================================
    # Init values
    # =========================================================================

    def InitValues(self):
        self.SetString(IDS.EDT_SERVER, self._server)
        self.SetBool(IDS.CHK_ONLY_VISIBLE, self._only_visible)

        # Refacet radios
        self.SetInt32(IDS.GRP_REFACET_RADIOS,
                      IDS.RDO_TRI if self._tri_mode else IDS.RDO_NGON)

        # Simple sliders
        self.SetFloat(IDS.SLD_TOLERANCE, self._tolerance,
                      min=0.0001, max=0.1, step=0.001, format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_ANGLE, self._angle,
                      min=0.01, max=1.57, step=0.01, format=c4d.FORMAT_FLOAT)

        # Advanced sliders
        self.SetFloat(IDS.SLD_MIN_WIDTH, self._min_width,
                      min=0.0, max=10.0, step=0.01, format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_MAX_WIDTH, self._max_width,
                      min=0.0, max=1000.0, step=0.1, format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_CURVE_CHORD_TOL, self._curve_chord_tol,
                      min=0.0001, max=1.0, step=0.001, format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_CURVE_CHORD_ANG, self._curve_chord_angle,
                      min=0.01, max=1.57, step=0.01, format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_SURF_PLANE_TOL, self._surface_plane_tol,
                      min=0.0001, max=1.0, step=0.001, format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_SURF_ANGLE_TOL, self._surface_angle_tol,
                      min=0.01, max=1.57, step=0.01, format=c4d.FORMAT_FLOAT)

        # Unit scale
        self.SetFloat(IDS.SLD_UNIT_SCALE, self._unit_scale,
                      min=0.0001, max=100.0, step=0.01, format=c4d.FORMAT_FLOAT)

        # Advanced group visibility
        self.SetBool(IDS.CHK_ADVANCED, self._show_advanced)
        self.HideElement(IDS.GRP_REFACET_ADVANCED, not self._show_advanced)

        self._update_ui_state()
        self.SetTimer(self.TIMER_INTERVAL)
        return True

    # =========================================================================
    # Timer — process bridge events on the main thread
    # =========================================================================

    def Timer(self, msg):
        """
        Called every TIMER_INTERVAL ms by Cinema 4D on the main thread.

        Bug 4 fix: _update_ui_state() is called unconditionally on every tick
        so that status messages written by StatusReporter (which updates
        bridge.status_message directly without pushing a queue event) appear
        immediately in the Status label.
        """
        count = self.bridge.process_pending_events()
        if count > 0:
            self._busy = False
        # Always refresh labels — catches status_message changes between events
        self._update_ui_state()

    # =========================================================================
    # Commands
    # =========================================================================

    def Command(self, id, msg):
        # ── Connection ───────────────────────────────────────────────────────
        if id == IDS.BTN_CONNECT:
            if self._busy:
                return True
            self._server = self.GetString(IDS.EDT_SERVER)
            self.client.connect(self._server)

        elif id == IDS.BTN_DISCONNECT:
            self.client.disconnect()

        # ── Refresh ──────────────────────────────────────────────────────────
        elif id == IDS.BTN_REFRESH:
            if self._busy:
                return True
            self._busy = True
            self._only_visible = self.GetBool(IDS.CHK_ONLY_VISIBLE)
            if self._only_visible:
                self.client.list_visible()
            else:
                self.client.list_all()

        # ── Live Link ────────────────────────────────────────────────────────
        elif id == IDS.CHK_LIVELINK:
            if self.GetBool(IDS.CHK_LIVELINK):
                self.client.subscribe_all()
            else:
                self.client.unsubscribe()

        # ── Unit Scale ───────────────────────────────────────────────────────
        elif id == IDS.SLD_UNIT_SCALE:
            self._unit_scale = self.GetFloat(IDS.SLD_UNIT_SCALE)
            self.handler.update_unit_scale(self._unit_scale)

        # ── Refacet radio ────────────────────────────────────────────────────
        elif id == IDS.GRP_REFACET_RADIOS:
            self._tri_mode = (self.GetInt32(IDS.GRP_REFACET_RADIOS) == IDS.RDO_TRI)

        # ── Advanced toggle ──────────────────────────────────────────────────
        elif id == IDS.CHK_ADVANCED:
            self._show_advanced = self.GetBool(IDS.CHK_ADVANCED)
            self.HideElement(IDS.GRP_REFACET_ADVANCED, not self._show_advanced)
            self.LayoutChanged(IDS.GRP_REFACET)

        # ── Refacet ──────────────────────────────────────────────────────────
        elif id == IDS.BTN_REFACET:
            if self._busy:
                return True
            self._busy = True
            self._do_refacet()

        return True

    # =========================================================================
    # UI state
    # =========================================================================

    def _update_ui_state(self):
        connected = self.bridge.connected

        self.Enable(IDS.BTN_CONNECT,    not connected and not self._busy)
        self.Enable(IDS.BTN_DISCONNECT, connected)
        self.Enable(IDS.BTN_REFRESH,    connected and not self._busy)
        self.Enable(IDS.CHK_LIVELINK,   connected)
        self.Enable(IDS.BTN_REFACET,    connected and not self._busy)
        self.Enable(IDS.EDT_SERVER,     not connected)

        # Status label — always current because Timer runs every tick
        status = self.bridge.status_message
        self.SetString(IDS.LBL_STATUS,   f"Status: {status}")
        filename = self.bridge.filename or "-"
        self.SetString(IDS.LBL_FILENAME, f"File: {filename}")

    # =========================================================================
    # Bridge event callbacks
    # =========================================================================

    def _on_connected(self, event: BridgeEvent):
        self._busy = False

    def _on_disconnected(self, event: BridgeEvent):
        self._busy = False
        self.SetBool(IDS.CHK_LIVELINK, False)

    def _on_connection_error(self, event: BridgeEvent):
        self._busy = False
        error = event.error_message or "Unknown error"
        gui.MessageDialog(f"Connection error:\n{error}")

    def _on_new_version(self, event: BridgeEvent):
        """
        Feature 8: Plasticity saved a new version.
        The handler already updated bridge.status_message with a 'refresh
        available' message, so the next Timer tick will display it automatically.
        No additional action needed here.
        """
        pass

    def _on_new_file(self, event: BridgeEvent):
        """
        Feature 8: Plasticity opened a different file.
        bridge.filename is updated by client._dispatch_parsed, and
        bridge.status_message by _on_new_file in the handler.
        The next Timer tick picks both up automatically.
        """
        pass

    # =========================================================================
    # Refacet
    # =========================================================================

    def _do_refacet(self):
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            self._busy = False
            return

        ids = self.handler.get_selected_plasticity_ids(doc)
        if not ids:
            gui.MessageDialog("No Plasticity objects selected.\n"
                              "Select one or more Plasticity mesh objects first.")
            self._busy = False
            return

        # Group selected IDs by filename
        by_filename = {}
        for filename, obj_id in ids:
            by_filename.setdefault(filename, []).append(obj_id)

        # Read simple mode values
        tolerance = self.GetFloat(IDS.SLD_TOLERANCE)
        angle     = self.GetFloat(IDS.SLD_ANGLE)
        min_width = self.GetFloat(IDS.SLD_MIN_WIDTH)
        max_width = self.GetFloat(IDS.SLD_MAX_WIDTH)

        # Matching the Blender addon exactly:
        #   Tri mode  → max_sides=3,   plane_angle=0.0
        #   Ngon mode → max_sides=128, plane_angle=π/4
        #   curve_chord_max = max_width * sqrt(0.5)  (Blender formula)
        max_sides       = 3   if self._tri_mode else 128
        plane_angle     = 0.0 if self._tri_mode else math.pi / 4.0
        curve_chord_max = max_width * math.sqrt(0.5)

        if self._show_advanced:
            cct = self.GetFloat(IDS.SLD_CURVE_CHORD_TOL)
            cca = self.GetFloat(IDS.SLD_CURVE_CHORD_ANG)
            spt = self.GetFloat(IDS.SLD_SURF_PLANE_TOL)
            spa = self.GetFloat(IDS.SLD_SURF_ANGLE_TOL)
        else:
            # Simple mode: single tolerance/angle for both curve and surface
            cct = tolerance
            cca = angle
            spt = tolerance
            spa = angle

        for filename, obj_ids in by_filename.items():
            self.client.refacet_some(
                filename              = filename,
                plasticity_ids        = obj_ids,
                relative_to_bbox      = True,
                curve_chord_tolerance = cct,
                curve_chord_angle     = cca,
                surface_plane_tolerance = spt,
                surface_plane_angle   = spa,
                match_topology        = True,
                max_sides             = max_sides,
                plane_angle           = plane_angle,
                min_width             = min_width,
                max_width             = max_width,
                curve_chord_max       = curve_chord_max,
                shape                 = FacetShapeType.CUT,
            )

    # =========================================================================
    # Layout restore and cleanup
    # =========================================================================

    def RestoreLayout(self, pluginid, secret):
        """
        Bug 1 fix: must call self.Restore(pluginid, secret), NOT self.Open().
        SDK: GeDialog.Restore() is the only correct method to use inside
        CommandData.RestoreLayout().  Calling Open() here would either open a
        duplicate dialog or silently fail after a layout restore.
        """
        return self.Restore(pluginid=pluginid, secret=secret)

    def DestroyWindow(self):
        self.SetTimer(0)
        if self.bridge.connected:
            self.client.disconnect()
        return super().DestroyWindow()