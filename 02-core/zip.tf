# --------------------------------------------------------------------------------
# DATA: archive_file.lambdas_zip
#
# Packages all Lambda source from code/ into one ZIP. The API Lambda and the SQS
# worker share this archive; each points at its own handler. boto3 is not in
# here -- it comes from the layer in lambdas.tf.
# --------------------------------------------------------------------------------
data "archive_file" "lambdas_zip" {
  type        = "zip"
  source_dir  = "${path.module}/code"
  output_path = "${path.module}/lambdas.zip"
}
