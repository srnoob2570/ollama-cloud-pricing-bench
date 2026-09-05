// The shared-formulas block's test-side executor: loads the block, an optional
// prelude (the page globals the wrappers read), then evaluates the job's
// expressions. Jobs ride on stdin as JSON: {file, prelude, exprs} -> JSON array.
const fs = require("fs");
const job = JSON.parse(fs.readFileSync(0, "utf8"));
const src = fs.readFileSync(job.file, "utf8");
const body =
    src +
    "\n" +
    (job.prelude || "") +
    "\nreturn [\n" +
    job.exprs
        .map(function (e) {
            return "  (" + e + "),\n";
        })
        .join("") +
    "];\n";
process.stdout.write(JSON.stringify(new Function(body)()));
