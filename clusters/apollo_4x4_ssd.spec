[clusters]
apollo =
    172.23.96.15:8091
    172.23.96.16:8091
    172.23.96.17:8091
    172.23.96.18:8091
thor =
    172.23.96.11:8091
    172.23.96.12:8091
    172.23.96.13:8091
    172.23.96.14:8091

[workers]
apollo_w1 =
    172.23.97.74
apollo_w2 =
    172.23.97.75

[storage]
data = /ssd
index = /ssd

[credentials]
rest = Administrator:password
ssh = root:couchbase

[parameters]
Platform = Physical
OS = CentOS 6.5
CPU = Intel Xeon E5-2630
Memory = 64 GB
Disk = 2 x SSD
