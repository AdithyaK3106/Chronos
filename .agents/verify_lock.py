import sys
import json
import sqlite3
from pathlib import Path

def main():
    try:
        payload = json.load(sys.stdin)
        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        
        target_file = args.get("TargetFile") or args.get("AbsolutePath")
        
        if not target_file:
            print(json.dumps({"decision": "allow"}))
            return
            
        # Get relative path from workspace root
        try:
            workspace_root = Path(payload["workspacePaths"][0])
            rel_path = str(Path(target_file).relative_to(workspace_root)).replace("\\", "/")
        except (ValueError, KeyError):
            print(json.dumps({"decision": "allow"}))
            return
            
        # Check chronos.db for an active lock on this path
        db_path = workspace_root / ".chronos" / "chronos.db"
        if not db_path.exists():
            print(json.dumps({"decision": "allow"}))
            return
            
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        
        # Chronos F3 intent locks. We just check if ANY lock exists for this file
        # that hasn't expired. In a real scenario we'd match the agent_id.
        cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='locks'")
        if cur.fetchone()[0] == 0:
            print(json.dumps({"decision": "allow"}))
            return
            
        # check if lock exists (simplified check)
        cur.execute("SELECT node_id FROM locks WHERE node_id = ? OR ? LIKE node_id || '%'", (rel_path, rel_path))
        row = cur.fetchone()
        con.close()
        
        if not row:
            print(json.dumps({
                "decision": "deny", 
                "reason": f"CHRONOS LOCK ENFORCEMENT: You must call chronos_acquire_lock for '{rel_path}' before modifying it!"
            }))
            return
            
        print(json.dumps({"decision": "allow"}))
        
    except Exception as e:
        # Fail open if the script crashes
        print(json.dumps({"decision": "allow"}))

if __name__ == "__main__":
    main()
