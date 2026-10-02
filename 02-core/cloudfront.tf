# ================================================================================
# Route53 zone lookup
# Derives parent zone from custom_domain by stripping the first label.
# e.g. "app.example.com" → looks up "example.com"
# ================================================================================

locals {
  zone_name = var.custom_domain != "" ? join(".", slice(split(".", var.custom_domain), 1, length(split(".", var.custom_domain)))) : ""
}

data "aws_route53_zone" "app" {
  count        = var.custom_domain != "" ? 1 : 0
  name         = local.zone_name
  private_zone = false
}

# ================================================================================
# ACM Certificate
# Only created when custom_domain is set.
# Must be in us-east-1 — CloudFront requires certificates in this region.
# ================================================================================

resource "aws_acm_certificate" "app" {
  count             = var.custom_domain != "" ? 1 : 0
  domain_name       = var.custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# ================================================================================
# Route53 DNS validation records
# Only created when custom_domain is set.
# ================================================================================

resource "aws_route53_record" "app_cert_validation" {
  for_each = var.custom_domain != "" ? {
    for dvo in aws_acm_certificate.app[0].domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id         = data.aws_route53_zone.app[0].zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# ================================================================================
# Wait for ACM to validate before creating CloudFront distribution
# Only created when custom_domain is set.
# ================================================================================

resource "aws_acm_certificate_validation" "app" {
  count                   = var.custom_domain != "" ? 1 : 0
  certificate_arn         = aws_acm_certificate.app[0].arn
  validation_record_fqdns = [for r in aws_route53_record.app_cert_validation : r.fqdn]
}

# ================================================================================
# CloudFront distribution
# Always created. Uses custom domain + ACM cert when custom_domain is set,
# otherwise serves from the default *.cloudfront.net HTTPS domain.
# ================================================================================

resource "aws_cloudfront_distribution" "app" {
  enabled             = true
  default_root_object = "index.html"
  aliases             = var.custom_domain != "" ? [var.custom_domain] : []

  origin {
    domain_name = "${aws_s3_bucket.frontend.bucket}.s3-website-${var.region}.amazonaws.com"
    origin_id   = "s3-frontend"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400
  }

  # Redirect SPA 403/404s to index.html for client-side routing
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }

  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Custom domain: use ACM certificate
  dynamic "viewer_certificate" {
    for_each = var.custom_domain != "" ? [1] : []
    content {
      acm_certificate_arn      = aws_acm_certificate_validation.app[0].certificate_arn
      ssl_support_method       = "sni-only"
      minimum_protocol_version = "TLSv1.2_2021"
    }
  }

  # No custom domain: use CloudFront's default *.cloudfront.net certificate
  dynamic "viewer_certificate" {
    for_each = var.custom_domain == "" ? [1] : []
    content {
      cloudfront_default_certificate = true
    }
  }
}

# ================================================================================
# Route53 A alias record → CloudFront distribution
# Only created when custom_domain is set.
# ================================================================================

resource "aws_route53_record" "app" {
  count   = var.custom_domain != "" ? 1 : 0
  zone_id = data.aws_route53_zone.app[0].zone_id
  name    = var.custom_domain
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.app.domain_name
    zone_id                = aws_cloudfront_distribution.app.hosted_zone_id
    evaluate_target_health = false
  }
}

# ================================================================================
# Outputs
# ================================================================================

output "custom_domain_url" {
  value = var.custom_domain != "" ? "https://${var.custom_domain}" : "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.app.id
}
