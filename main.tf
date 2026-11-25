terraform {
  required_version = ">= 1.5.0"
}

# Request a plan change on every deployment so an empty plan cannot skip approval.
# This resource only records a timestamp in Terraform state.
resource "terraform_data" "maintenance_run" {
  input = timestamp()
}
