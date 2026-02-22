"""
Main dialog for Plasticity Bridge plugin.

UI uses Cinema 4D's native QUICKTAB interface:
  Three tabs — Server, Basic, Utilities — with QUICKTAB_BAR section headers
  before each tab's content for visual separation when multiple tabs are open.
  Shift-click opens multiple tabs simultaneously, exactly like native C4D.
  All label+input pairs are in unified 2-column grids for proper alignment.
"""

import c4d
import math
from c4d import gui
from modules.threading_bridge import EventType, BridgeEvent
from modules.protocol import FacetShapeType

PLUGIN_ID = 1066929

# Tab indices for the QuickTab
TAB_SERVER    = 0
TAB_BASIC     = 1
TAB_UTILITIES = 2


class IDS:
    # Top-level
    GRP_MAIN             = 1000

    # QuickTab bar
    QUICKTAB             = 1010

    # Tab content groups (shown / hidden by quicktab selection)
    GRP_TAB_SERVER       = 1020
    GRP_TAB_BASIC        = 1021
    GRP_TAB_UTILITIES    = 1022

    # Section headers (QUICKTAB_BAR)
    HDR_SERVER           = 1030
    HDR_BASIC            = 1031
    HDR_UTILITIES        = 1032

    # Server tab
    EDT_SERVER           = 1101
    BTN_CONNECT          = 1102
    BTN_DISCONNECT       = 1103
    LBL_STATUS           = 1200
    LBL_FILENAME         = 1201
    CHK_ONLY_VISIBLE     = 1300
    BTN_REFRESH          = 1301
    CHK_LIVELINK         = 1400
    SLD_UNIT_SCALE       = 1800

    # Basic tab (refacet)
    TOGGLE_TOPOLOGY      = 1500
    TOGGLE_REFACET_OPTS  = 1501
    GRP_SIMPLE_OPTIONS   = 1502
    GRP_ADVANCED_OPTIONS = 1503
    SLD_TOLERANCE        = 1504
    SLD_ANGLE            = 1505
    SLD_MIN_WIDTH        = 1600
    SLD_MAX_WIDTH        = 1601
    SLD_CURVE_CHORD_TOL  = 1602
    SLD_CURVE_CHORD_ANG  = 1603
    SLD_SURF_PLANE_TOL   = 1604
    SLD_SURF_ANGLE_TOL   = 1605
    BTN_REFACET          = 1700

    # Utilities tab
    LBL_UTILITIES_SOON   = 1900


def _add_section_header(dlg, gadget_id, title):
    """Add a QUICKTAB_BAR section header — the thin titled separator bar."""
    bc = c4d.BaseContainer()
    bc.SetBool(c4d.QUICKTAB_BAR, True)
    bc.SetString(c4d.QUICKTAB_BARTITLE, title)
    dlg.AddCustomGui(gadget_id, c4d.CUSTOMGUI_QUICKTAB, "",
                     c4d.BFH_SCALEFIT, 0, 0, bc)


class PlasticityDialog(gui.GeDialog):
    TIMER_INTERVAL = 16   # ~60 fps

    def __init__(self, client, handler, bridge):
        super().__init__()
        self.client   = client
        self.handler  = handler
        self.bridge   = bridge

        # Dialog state
        self._server            = "localhost:8980"
        self._only_visible      = False
        self._tri_mode          = True
        self._advanced_mode     = False
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

        # QuickTab widget references
        self._quicktab          = None
        self._toggle_topology   = None
        self._toggle_refacet    = None

        # Register bridge callbacks
        bridge.register_callback(EventType.CONNECTED,        self._on_connected)
        bridge.register_callback(EventType.DISCONNECTED,     self._on_disconnected)
        bridge.register_callback(EventType.CONNECTION_ERROR, self._on_connection_error)
        bridge.register_callback(EventType.NEW_VERSION,      self._on_new_version)
        bridge.register_callback(EventType.NEW_FILE,         self._on_new_file)
        bridge.register_callback(EventType.LIST_RESPONSE,    self._on_operation_complete)
        bridge.register_callback(EventType.REFACET_RESPONSE, self._on_operation_complete)
        bridge.register_callback(EventType.STATUS_UPDATE,    self._on_status_update)

    # =========================================================================
    # Layout
    # =========================================================================

    # Fixed width for all left-column labels — ensures the right column
    # (inputs, toggles, buttons) starts at the same x-position throughout.
    LABEL_W = 160

    def CreateLayout(self):
        self.SetTitle("Plasticity Bridge")
        LW = self.LABEL_W

        if self.GroupBegin(IDS.GRP_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                           cols=1, rows=0):
            self.GroupBorderSpace(4, 4, 4, 4)

            # ── QuickTab bar ─────────────────────────────────────────────
            bc = c4d.BaseContainer()
            bc.SetBool(c4d.QUICKTAB_BAR, False)
            bc.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
            bc.SetBool(c4d.QUICKTAB_NOMULTISELECT, False)

            self._quicktab = self.AddCustomGui(
                IDS.QUICKTAB, c4d.CUSTOMGUI_QUICKTAB, "",
                c4d.BFH_SCALEFIT | c4d.BFV_FIT, 0, 0, bc)

            if self._quicktab:
                self._quicktab.AppendString(TAB_SERVER,    "Server",    True)
                self._quicktab.AppendString(TAB_BASIC,     "Basic",     True)
                self._quicktab.AppendString(TAB_UTILITIES, "Utilities", False)

            # ── Tab: Server ──────────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_TAB_SERVER,
                               c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0):

                _add_section_header(self, IDS.HDR_SERVER, "Server")

                # Server address
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 4, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Server")
                    self.AddEditText(IDS.EDT_SERVER, c4d.BFH_SCALEFIT)
                self.GroupEnd()

                # Connect / Disconnect — spacer + 2 buttons
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 0, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW, name="")
                    if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                        self.AddButton(IDS.BTN_CONNECT, c4d.BFH_SCALEFIT,
                                       name="Connect")
                        self.AddButton(IDS.BTN_DISCONNECT, c4d.BFH_SCALEFIT,
                                       name="Disconnect")
                    self.GroupEnd()
                self.GroupEnd()

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Status
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0):
                    self.GroupBorderSpace(4, 2, 4, 2)
                    self.AddStaticText(IDS.LBL_STATUS, c4d.BFH_SCALEFIT,
                                       name="Status: Disconnected")
                    self.AddStaticText(IDS.LBL_FILENAME, c4d.BFH_SCALEFIT,
                                       name="File: -")
                self.GroupEnd()

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Only Visible + Refresh
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 0)
                    self.AddCheckbox(IDS.CHK_ONLY_VISIBLE, c4d.BFH_LEFT,
                                     initw=LW, inith=0, name="Only Visible")
                    self.AddButton(IDS.BTN_REFRESH, c4d.BFH_SCALEFIT,
                                   name="Refresh")
                self.GroupEnd()

                # Live Link
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0):
                    self.GroupBorderSpace(4, 0, 4, 2)
                    self.AddCheckbox(IDS.CHK_LIVELINK, c4d.BFH_LEFT,
                                     initw=0, inith=0, name="Live Link")
                self.GroupEnd()

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Unit Scale
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Unit Scale")
                    self.AddEditSlider(IDS.SLD_UNIT_SCALE, c4d.BFH_SCALEFIT)
                self.GroupEnd()

            self.GroupEnd()  # GRP_TAB_SERVER

            # ── Tab: Basic (refacet) ─────────────────────────────────────
            if self.GroupBegin(IDS.GRP_TAB_BASIC,
                               c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0):

                _add_section_header(self, IDS.HDR_BASIC, "Basic")

                # Topology toggle
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 4, 4, 2)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Topology")
                    bc_topo = c4d.BaseContainer()
                    bc_topo.SetBool(c4d.QUICKTAB_BAR, False)
                    bc_topo.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
                    bc_topo.SetBool(c4d.QUICKTAB_NOMULTISELECT, True)
                    self._toggle_topology = self.AddCustomGui(
                        IDS.TOGGLE_TOPOLOGY, c4d.CUSTOMGUI_QUICKTAB, "",
                        c4d.BFH_SCALEFIT | c4d.BFV_FIT, 0, 0, bc_topo)
                    if self._toggle_topology:
                        self._toggle_topology.AppendString(0, "Tris",  True)
                        self._toggle_topology.AppendString(1, "Ngons", False)
                self.GroupEnd()

                # Refacet Options toggle
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Refacet Options")
                    bc_opts = c4d.BaseContainer()
                    bc_opts.SetBool(c4d.QUICKTAB_BAR, False)
                    bc_opts.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
                    bc_opts.SetBool(c4d.QUICKTAB_NOMULTISELECT, True)
                    self._toggle_refacet = self.AddCustomGui(
                        IDS.TOGGLE_REFACET_OPTS, c4d.CUSTOMGUI_QUICKTAB,
                        "", c4d.BFH_SCALEFIT | c4d.BFV_FIT, 0, 0, bc_opts)
                    if self._toggle_refacet:
                        self._toggle_refacet.AppendString(0, "Simple",   True)
                        self._toggle_refacet.AppendString(1, "Advanced", False)
                self.GroupEnd()

                # Simple options group (Tolerance + Angle)
                if self.GroupBegin(IDS.GRP_SIMPLE_OPTIONS,
                                   c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 2)
                    self.GroupBorderSpace(4, 2, 4, 0)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Tolerance")
                    self.AddEditSlider(IDS.SLD_TOLERANCE, c4d.BFH_SCALEFIT)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Angle")
                    self.AddEditSlider(IDS.SLD_ANGLE, c4d.BFH_SCALEFIT)
                self.GroupEnd()

                # Advanced options group
                if self.GroupBegin(IDS.GRP_ADVANCED_OPTIONS,
                                   c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 2)
                    self.GroupBorderSpace(4, 2, 4, 0)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Min Width")
                    self.AddEditSlider(IDS.SLD_MIN_WIDTH, c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Max Width")
                    self.AddEditSlider(IDS.SLD_MAX_WIDTH, c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Edge Chord Tol")
                    self.AddEditSlider(IDS.SLD_CURVE_CHORD_TOL,
                                       c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Edge Chord Angle")
                    self.AddEditSlider(IDS.SLD_CURVE_CHORD_ANG,
                                       c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Face Plane Tol")
                    self.AddEditSlider(IDS.SLD_SURF_PLANE_TOL,
                                       c4d.BFH_SCALEFIT)

                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW,
                                       name="Face Angle Tol")
                    self.AddEditSlider(IDS.SLD_SURF_ANGLE_TOL,
                                       c4d.BFH_SCALEFIT)
                self.GroupEnd()  # GRP_ADVANCED_OPTIONS

                self.AddSeparatorH(c4d.BFH_SCALEFIT)

                # Refacet Selected — spacer + button
                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0):
                    self.GroupSpace(8, 0)
                    self.GroupBorderSpace(4, 2, 4, 4)
                    self.AddStaticText(0, c4d.BFH_LEFT, initw=LW, name="")
                    self.AddButton(IDS.BTN_REFACET, c4d.BFH_SCALEFIT,
                                   name="Refacet Selected")
                self.GroupEnd()

            self.GroupEnd()  # GRP_TAB_BASIC

            # ── Tab: Utilities ───────────────────────────────────────────
            if self.GroupBegin(IDS.GRP_TAB_UTILITIES,
                               c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0):

                _add_section_header(self, IDS.HDR_UTILITIES, "Utilities")

                if self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0):
                    self.GroupBorderSpace(4, 12, 4, 12)
                    self.AddStaticText(
                        IDS.LBL_UTILITIES_SOON, c4d.BFH_CENTER,
                        name="Select Face / Mark Edges / Paint Faces"
                             " — coming soon")
                self.GroupEnd()

            self.GroupEnd()  # GRP_TAB_UTILITIES

        self.GroupEnd()  # GRP_MAIN
        return True

    # =========================================================================
    # Init values
    # =========================================================================

    def InitValues(self):
        self.SetString(IDS.EDT_SERVER, self._server)
        self.SetBool(IDS.CHK_ONLY_VISIBLE, self._only_visible)

        # Simple sliders
        self.SetFloat(IDS.SLD_TOLERANCE, self._tolerance,
                      min=0.0001, max=0.1, step=0.001,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_ANGLE, self._angle,
                      min=0.01, max=1.57, step=0.01,
                      format=c4d.FORMAT_FLOAT)

        # Advanced sliders
        self.SetFloat(IDS.SLD_MIN_WIDTH, self._min_width,
                      min=0.0, max=10.0, step=0.01,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_MAX_WIDTH, self._max_width,
                      min=0.0, max=1000.0, step=0.1,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_CURVE_CHORD_TOL, self._curve_chord_tol,
                      min=0.0001, max=1.0, step=0.001,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_CURVE_CHORD_ANG, self._curve_chord_angle,
                      min=0.01, max=1.57, step=0.01,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_SURF_PLANE_TOL, self._surface_plane_tol,
                      min=0.0001, max=1.0, step=0.001,
                      format=c4d.FORMAT_FLOAT)
        self.SetFloat(IDS.SLD_SURF_ANGLE_TOL, self._surface_angle_tol,
                      min=0.01, max=1.57, step=0.01,
                      format=c4d.FORMAT_FLOAT)

        # Unit scale
        self.SetFloat(IDS.SLD_UNIT_SCALE, self._unit_scale,
                      min=0.0001, max=100.0, step=0.01,
                      format=c4d.FORMAT_FLOAT)

        # Refacet options: show Simple group, hide Advanced group by default
        self._sync_refacet_options()

        # Apply initial quicktab visibility
        self._sync_tab_visibility()

        self._update_ui_state()
        self.SetTimer(self.TIMER_INTERVAL)
        return True

    # =========================================================================
    # QuickTab visibility sync
    # =========================================================================

    def _sync_tab_visibility(self):
        """Show / hide the three content groups to match the QuickTab state."""
        if not self._quicktab:
            return

        show_server    = self._quicktab.IsSelected(TAB_SERVER)
        show_basic     = self._quicktab.IsSelected(TAB_BASIC)
        show_utilities = self._quicktab.IsSelected(TAB_UTILITIES)

        self.HideElement(IDS.GRP_TAB_SERVER,    not show_server)
        self.HideElement(IDS.GRP_TAB_BASIC,     not show_basic)
        self.HideElement(IDS.GRP_TAB_UTILITIES, not show_utilities)

        self.LayoutChanged(IDS.GRP_MAIN)

    def _sync_refacet_options(self):
        """Show Simple or Advanced options based on the Refacet Options toggle."""
        if self._toggle_refacet:
            self._advanced_mode = self._toggle_refacet.IsSelected(1)
        self.HideElement(IDS.GRP_SIMPLE_OPTIONS,   self._advanced_mode)
        self.HideElement(IDS.GRP_ADVANCED_OPTIONS,  not self._advanced_mode)
        self.LayoutChanged(IDS.GRP_TAB_BASIC)

    # =========================================================================
    # Timer
    # =========================================================================

    def Timer(self, msg):
        self.bridge.process_pending_events()
        self._update_ui_state()

    # =========================================================================
    # Commands
    # =========================================================================

    def Command(self, id, msg):
        # ── QuickTab toggled ─────────────────────────────────────────────
        if id == IDS.QUICKTAB:
            self._sync_tab_visibility()
            return True

        # ── Connection ───────────────────────────────────────────────────
        if id == IDS.BTN_CONNECT:
            if self._busy:
                return True
            self._server = self.GetString(IDS.EDT_SERVER)
            self.client.connect(self._server)

        elif id == IDS.BTN_DISCONNECT:
            self.client.disconnect()

        # ── Refresh ──────────────────────────────────────────────────────
        elif id == IDS.BTN_REFRESH:
            if self._busy:
                return True
            self._busy = True
            self._only_visible = self.GetBool(IDS.CHK_ONLY_VISIBLE)
            if self._only_visible:
                self.client.list_visible()
            else:
                self.client.list_all()

        # ── Live Link ────────────────────────────────────────────────────
        elif id == IDS.CHK_LIVELINK:
            if self.GetBool(IDS.CHK_LIVELINK):
                self.client.subscribe_all()
            else:
                self.client.unsubscribe()

        # ── Unit Scale ───────────────────────────────────────────────────
        elif id == IDS.SLD_UNIT_SCALE:
            self._unit_scale = self.GetFloat(IDS.SLD_UNIT_SCALE)
            self.handler.update_unit_scale(self._unit_scale)

        # ── Topology toggle ───────────────────────────────────────────
        elif id == IDS.TOGGLE_TOPOLOGY:
            if self._toggle_topology:
                self._tri_mode = self._toggle_topology.IsSelected(0)

        # ── Refacet Options toggle ───────────────────────────────────
        elif id == IDS.TOGGLE_REFACET_OPTS:
            self._sync_refacet_options()

        # ── Refacet ──────────────────────────────────────────────────────
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

    def _on_operation_complete(self, event: BridgeEvent):
        self._busy = False

    def _on_status_update(self, event: BridgeEvent):
        self._busy = False

    def _on_new_version(self, event: BridgeEvent):
        pass

    def _on_new_file(self, event: BridgeEvent):
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
            gui.MessageDialog(
                "No Plasticity objects selected.\n"
                "Select one or more Plasticity mesh objects first.")
            self._busy = False
            return

        by_filename = {}
        for filename, obj_id in ids:
            by_filename.setdefault(filename, []).append(obj_id)

        tolerance = self.GetFloat(IDS.SLD_TOLERANCE)
        angle     = self.GetFloat(IDS.SLD_ANGLE)
        min_width = self.GetFloat(IDS.SLD_MIN_WIDTH)
        max_width = self.GetFloat(IDS.SLD_MAX_WIDTH)

        max_sides       = 3   if self._tri_mode else 128
        plane_angle     = 0.0 if self._tri_mode else math.pi / 4.0
        curve_chord_max = max_width * math.sqrt(0.5)

        if self._advanced_mode:
            cct = self.GetFloat(IDS.SLD_CURVE_CHORD_TOL)
            cca = self.GetFloat(IDS.SLD_CURVE_CHORD_ANG)
            spt = self.GetFloat(IDS.SLD_SURF_PLANE_TOL)
            spa = self.GetFloat(IDS.SLD_SURF_ANGLE_TOL)
        else:
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
        return self.Restore(pluginid=pluginid, secret=secret)

    def DestroyWindow(self):
        self.SetTimer(0)
        if self.bridge.connected:
            self.client.disconnect()
        return super().DestroyWindow()