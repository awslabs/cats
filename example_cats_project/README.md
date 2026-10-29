# Example CATS Project

A small, complete CATS project: one account, one network target, three tests. Use it as a
reference for the shape of `cats.yaml` and of the tests, or deploy it into an account you
control to see a full deploy → test → destroy cycle.

`cats.yaml` carries the placeholder account ID `123456789012` in `eu-west-1`: replace both
occurrences with your account, then run `cats generate-types` to regenerate `cats_types.py`
for the new account keyname. The `$schema` comment on the first line points at the
`.schema.json` that `cats validate -c example_cats_project/cats.yaml` writes beside the
config (the file is gitignored; run validate once to create it), so editors with a YAML
language server get completion and validation.

## Running it

With credentials for the account in `cats.yaml`:

```bash
# Deploy
CATS_CONFIG=example_cats_project/cats.yaml uv run cats infra deploy --all

# Run tests
CATS_CONFIG=example_cats_project/cats.yaml uv run cats test example_cats_project -vv -s

# Destroy
CATS_CONFIG=example_cats_project/cats.yaml uv run cats infra destroy --all
```

## What it deploys

The VPC and subnet come from SSM parameters (`/cats/integ-test/vpc-id` and
`/cats/integ-test/subnet-id`); they stand for any parameters naming a VPC and a subnet of your
own, and literal IDs work in their place. The stack then holds:

- an IAM test role, assumed by the tests
- an S3 bucket for UDP receipts
- an EC2 listener on TCP 8080 and UDP 5000, which is also where the probes run: the
  config enables only the default `ec2` handler, so there is no probe Lambda unless you
  add `lambda` to `enabled_network_handlers`
- an S3 gateway endpoint in the VPC

## What the tests prove

`test_network.py` has the listener probe itself over HTTP, TCP and UDP, and confirms the
UDP datagram arrived by reading its receipt from the bucket. They are the same assertions
you would write against a real workload; only the target changes.
