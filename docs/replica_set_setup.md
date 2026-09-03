# MongoDB Replica Set — Local Dev Setup

3-node replica set via Docker Compose, required before any of the adaptive layer's write/read concern logic is observable (a standalone `mongod` can't demonstrate any of it).

## One-time setup

**1. Add hosts file entries** (Windows: `C:\Windows\System32\drivers\etc\hosts`, edit as Administrator):

```
127.0.0.1 mongo1
127.0.0.1 mongo2
127.0.0.1 mongo3
```

**Why:** each node needs to resolve consistently from two different places — from *inside* the Docker network (where `mongo1`/`mongo2`/`mongo3` are the container service names) and from the *host* machine (where our actual Python code runs, e.g. `pytest`, `demo_app.py`). Each container listens on its own distinct port, identical inside and outside Docker (`mongo1:27017`, `mongo2:27018`, `mongo3:27019` — see `docker-compose.yml`), so once these hostnames resolve to `127.0.0.1` on the host, the exact same `host:port` pairs work correctly from both sides. No further changes needed after this one-time edit.

**2. Start the containers:**

```bash
docker compose up -d
```

First run pulls the `mongo:7.0` image (~264MB) — subsequent starts are fast.

**3. Initiate the replica set** (only needed once per fresh volume — skip this if containers were already initiated before and you're just restarting):

```bash
docker exec mongo1 mongosh --port 27017 --quiet --eval '
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongo1:27017" },
    { _id: 1, host: "mongo2:27018" },
    { _id: 2, host: "mongo3:27019" }
  ]
})
'
```

**4. Verify:**

```bash
docker exec mongo1 mongosh --port 27017 --quiet --eval '
var s = rs.status();
s.members.forEach(function(m) { print(m.name + " -> " + m.stateStr); });
'
```

Expect one `PRIMARY` and two `SECONDARY`.

## Connection string (for pymongo / AdaptiveClient)

```
mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0
```

## Smoke test (already verified working, both from inside a container and from the host via pymongo)

```python
from pymongo import MongoClient, ReadPreference
from pymongo.write_concern import WriteConcern

client = MongoClient("mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0")

db = client.get_database("smoke_test", write_concern=WriteConcern(w="majority"))
db.smoke.insert_one({"hello": "world"})

secondary_db = client.get_database("smoke_test", read_preference=ReadPreference.SECONDARY)
print(secondary_db.smoke.find_one({"hello": "world"}))
```

## Tearing down

```bash
docker compose down        # stop containers, keep data volumes
docker compose down -v     # stop containers AND wipe data (re-initiate needed after this)
```
