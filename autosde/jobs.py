"""Background work: threads in the local app, asynchronous worker invocations in AWS.

A job is a name and JSON-serializable arguments; `App.run_job` does the work either way.
"""

import json
import threading


class ThreadJobs:
    def __init__(self, run):
        self.run = run

    def submit(self, name, **args):
        threading.Thread(target=self.run, args=(name, args), daemon=True).start()


class InlineJobs:
    """Run immediately in the caller (tests and scripts)."""

    def __init__(self, run):
        self.run = run

    def submit(self, name, **args):
        self.run(name, args)


class LambdaJobs:
    """Invoke the worker function asynchronously (InvocationType=Event)."""

    def __init__(self, client, function_name):
        self.client, self.function_name = client, function_name

    def submit(self, name, **args):
        self.client.invoke(FunctionName=self.function_name, InvocationType="Event",
                           Payload=json.dumps({"job": name, "args": args}).encode())
