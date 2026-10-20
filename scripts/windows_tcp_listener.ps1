# Read Windows' native owner table instead of starting a CIM query for every
# 200 ms readiness poll. The PID remains part of the evidence: an open port
# alone must never authorize adoption or termination of a process.
if (-not ('SelectionTcpOwnerTable' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

public sealed class SelectionTcpListener {
    public int LocalPort { get; set; }
    public int OwningProcess { get; set; }
}

public static class SelectionTcpOwnerTable {
    [DllImport("iphlpapi.dll", SetLastError = true)]
    static extern uint GetExtendedTcpTable(IntPtr table, ref int size,
        bool order, int family, int tableClass, uint reserved);

    public static SelectionTcpListener[] Read(int port) {
        var listeners = new List<SelectionTcpListener>();
        foreach (int family in new int[] { 2, 23 }) {
            int size = 0;
            uint result = GetExtendedTcpTable(IntPtr.Zero, ref size, false, family, 3, 0);
            if (result != 0 && result != 122) throw new Win32Exception((int)result);
            // A process can open another listener while the first buffer is
            // sized; retry only the native buffer allocation in that case.
            for (int attempt = 0; attempt < 4; attempt++) {
                IntPtr buffer = Marshal.AllocHGlobal(Math.Max(size, 4));
                try {
                    result = GetExtendedTcpTable(buffer, ref size, false, family, 3, 0);
                    if (result == 122) continue;
                    if (result != 0) throw new Win32Exception((int)result);
                    int rows = Marshal.ReadInt32(buffer);
                    int rowSize = family == 2 ? 24 : 56;
                    int portOffset = family == 2 ? 8 : 20;
                    int pidOffset = family == 2 ? 20 : 52;
                    for (int index = 0; index < rows; index++) {
                        IntPtr row = IntPtr.Add(buffer, 4 + index * rowSize);
                        int localPort = (Marshal.ReadByte(row, portOffset) << 8)
                            | Marshal.ReadByte(row, portOffset + 1);
                        if (port > 0 && localPort != port) continue;
                        listeners.Add(new SelectionTcpListener {
                            LocalPort = localPort,
                            OwningProcess = Marshal.ReadInt32(row, pidOffset)
                        });
                    }
                    break;
                } finally { Marshal.FreeHGlobal(buffer); }
            }
            if (result != 0) throw new Win32Exception((int)result);
        }
        return listeners.ToArray();
    }
}
'@
}

function Get-LocalTcpListener {
    param([ValidateRange(0, 65535)][int]$LocalPort = 0)
    [SelectionTcpOwnerTable]::Read($LocalPort)
}

function Get-ListenerProcessId {
    param([Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port)
    Get-LocalTcpListener -LocalPort $Port | Select-Object -ExpandProperty OwningProcess -Unique
}
