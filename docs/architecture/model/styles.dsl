// This repository's styles. The shared meanings (people, systems, external,
// shapes, security markings, arrows) and the approved palette come from
// styles-shared.dsl, copied unchanged from development-base. This file only
// maps this system's layers and groups onto palette families, plus local
// extras. Tag order matters on an element: layer tag first, security marking
// last.
//
// Three layers only: what runs (Run), what is stored (Data), what the outside
// world reaches (Deliver).

styles {
    !include styles-shared.dsl

    // Layers: Run green, Data slate, Deliver purple.
    element "Layer Run" {
        background ${GREEN_FILL}
        stroke ${GREEN_STROKE}
    }
    relationship "Layer Run" {
        color ${GREEN_STROKE}
    }
    element "Layer Data" {
        background ${SLATE_FILL}
        stroke ${SLATE_STROKE}
    }
    relationship "Layer Data" {
        color ${SLATE_STROKE}
    }
    element "Layer Deliver" {
        background ${PURPLE_FILL}
        stroke ${PURPLE_STROKE}
    }
    relationship "Layer Deliver" {
        color ${PURPLE_STROKE}
    }

    // Groups mark trust boundaries; their tint follows the layer they hold.
    element "Group:Azure subscription (private)" {
        color ${SLATE_LABEL}
        stroke ${SLATE_STROKE}
        background ${SLATE_FRAME}
    }
    element "Group:Cloudflare edge (internet-facing)" {
        color ${PURPLE_LABEL}
        stroke ${PURPLE_STROKE}
        background ${PURPLE_FRAME}
    }
}
