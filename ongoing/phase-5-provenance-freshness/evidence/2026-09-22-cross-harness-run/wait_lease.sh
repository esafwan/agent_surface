until python3 -c "
from surface.store import Store
s = Store('$SB/state.sqlite3')
row = s.conn.execute(\"SELECT status FROM events WHERE id='evt_10ae369c'\").fetchone()
import sys
sys.exit(0 if row and row[0]=='pending' else 1)
" 2>/dev/null; do
  cd <repo-root>
  python3 -m surface.cli --db $SB/state.sqlite3 recover --force-reclaim > /dev/null 2>&1
  sleep 5
done
echo "LEASE_RECLAIMED"
