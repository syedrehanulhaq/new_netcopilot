TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_top_flapping_links",
            "description": "Get top N flapping links (Prometheus-backed when configured)",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "since_hours": {"type": "integer"},
                },
                "required": ["limit", "since_hours"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_interface_health",
            "description": "Fetch interface health via SSH/CLI (IOS XR or Junos)",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_interface_status",
            "description": "Fetch status of all interfaces on a device",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                },
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_link_health",
            "description": "Check link health from both ends using LLDP neighbor discovery",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_isis_health",
            "description": "Investigate ISIS issues using neighbor, link, and MTU checks",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_interface_counters",
            "description": "Fetch interface counters snapshot via show interface",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                    "window_minutes": {"type": "integer"},
                },
                "required": ["device", "interface", "window_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create a ticket with findings (mocked)",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_type": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "team": {"type": "string"},
                    "priority": {"type": "string"},
                },
                "required": ["ticket_type", "summary", "description", "team", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_ticket",
            "description": "Close a Jira ticket by key",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "transition_name": {"type": "string"},
                },
                "required": ["issue_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_ticket_comment",
            "description": "Add a comment to a Jira ticket",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["issue_key", "comment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bgp_neighbor_status",
            "description": "Get BGP neighbor state/details",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "neighbor": {"type": "string"},
                },
                "required": ["device", "neighbor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ospf_neighbors",
            "description": "Get OSPF neighbor table",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string"}},
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_isis_neighbors",
            "description": "Get IS-IS neighbor table",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string"}},
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cpu_memory",
            "description": "Get CPU and memory summary",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string"}},
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chassis_health",
            "description": "Get chassis health (FPC/PIC/RE/SIB) from Junos",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string"}},
                "required": ["device"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route_lookup",
            "description": "Lookup a route for a prefix",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "prefix": {"type": "string"},
                },
                "required": ["device", "prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bgp_route",
            "description": "Lookup BGP route for a prefix",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "prefix": {"type": "string"},
                },
                "required": ["device", "prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_acl_hits",
            "description": "Show ACL hits/counters",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "acl_name": {"type": "string"},
                },
                "required": ["device", "acl_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mtu_info",
            "description": "Show interface MTU details",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "interface": {"type": "string"},
                },
                "required": ["device", "interface"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ping",
            "description": "Run ping from device",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "destination": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["device", "destination", "count"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_traceroute",
            "description": "Run traceroute from device",
            "parameters": {
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["device", "destination"],
            },
        },
    },
]
