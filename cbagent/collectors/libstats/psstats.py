from cbagent.collectors.libstats.remotestats import RemoteStats, parallel_task


class PSStats(RemoteStats):

    METRICS = (
        ("rss", 1024),    # kB -> B
        ("vsize", 1024),
    )

    PS_CMD = "ps -eo pid,rss,vsize,comm | " \
        "grep {} | grep -v grep | sort -n -k 2 | tail -n 1"

    TOP_CMD = "top -b n2 -d1 -p {0} | grep '^\s*{0}'"

    @parallel_task(server_side=True)
    def get_server_samples(self, process):
        return self.get_samples(process)

    @parallel_task(server_side=False)
    def get_client_samples(self, process):
        return self.get_samples(process)

    def get_samples(self, process):
        samples = {}

        stdout = self.run(self.PS_CMD.format(process))
        if stdout:
            for i, value in enumerate(stdout.split()[1:1 + len(self.METRICS)]):
                metric, multiplier = self.METRICS[i]
                title = "{}_{}".format(process, metric)
                samples[title] = float(value) * multiplier
            pid = stdout.split()[0]
        else:
            return samples

        stdout = self.run(self.TOP_CMD.format(pid))
        if stdout:
            title = "{}_cpu".format(process)
            samples[title] = float(stdout.split()[8])
        return samples
