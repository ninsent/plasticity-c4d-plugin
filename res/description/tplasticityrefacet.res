CONTAINER Tplasticityrefacet
{
    NAME Tplasticityrefacet;
    INCLUDE Tbase;

    GROUP ID_TAGPROPERTIES
    {
        LONG PLASTICITYREFACET_TOPOLOGY
        {
            CYCLE
            {
                PLASTICITYREFACET_TOPOLOGY_TRIS;
                PLASTICITYREFACET_TOPOLOGY_NGONS;
            }
        }

        LONG PLASTICITYREFACET_OPTIONS_MODE
        {
            CYCLE
            {
                PLASTICITYREFACET_OPTIONS_SIMPLE;
                PLASTICITYREFACET_OPTIONS_ADVANCED;
            }
        }

        SEPARATOR { LINE; }

        GROUP PLASTICITYREFACET_GRP_SIMPLE
        {
            DEFAULT 1;

            REAL PLASTICITYREFACET_TOLERANCE
            {
                UNIT REAL;
                MIN 0.0001; MAX 0.1; STEP 0.001;
                MINSLIDER 0.0001; MAXSLIDER 0.1;
                CUSTOMGUI REALSLIDER;
            }
            REAL PLASTICITYREFACET_ANGLE
            {
                UNIT REAL;
                MIN 0.01; MAX 1.57; STEP 0.01;
                MINSLIDER 0.01; MAXSLIDER 1.57;
                CUSTOMGUI REALSLIDER;
            }
        }

        GROUP PLASTICITYREFACET_GRP_ADVANCED
        {
            DEFAULT 1;

            REAL PLASTICITYREFACET_MIN_WIDTH
            {
                UNIT REAL;
                MIN 0.0; MAX 10.0; STEP 0.01;
                MINSLIDER 0.0; MAXSLIDER 10.0;
                CUSTOMGUI REALSLIDER;
            }
            REAL PLASTICITYREFACET_MAX_WIDTH
            {
                UNIT REAL;
                MIN 0.0; MAX 1000.0; STEP 0.1;
                MINSLIDER 0.0; MAXSLIDER 1000.0;
                CUSTOMGUI REALSLIDER;
            }
            REAL PLASTICITYREFACET_CURVE_CHORD_TOL
            {
                UNIT REAL;
                MIN 0.0001; MAX 1.0; STEP 0.001;
                MINSLIDER 0.0001; MAXSLIDER 1.0;
                CUSTOMGUI REALSLIDER;
            }
            REAL PLASTICITYREFACET_CURVE_CHORD_ANG
            {
                UNIT REAL;
                MIN 0.01; MAX 1.57; STEP 0.01;
                MINSLIDER 0.01; MAXSLIDER 1.57;
                CUSTOMGUI REALSLIDER;
            }
            REAL PLASTICITYREFACET_SURF_PLANE_TOL
            {
                UNIT REAL;
                MIN 0.0001; MAX 1.0; STEP 0.001;
                MINSLIDER 0.0001; MAXSLIDER 1.0;
                CUSTOMGUI REALSLIDER;
            }
            REAL PLASTICITYREFACET_SURF_ANGLE_TOL
            {
                UNIT REAL;
                MIN 0.01; MAX 1.57; STEP 0.01;
                MINSLIDER 0.01; MAXSLIDER 1.57;
                CUSTOMGUI REALSLIDER;
            }
        }
    }
}
