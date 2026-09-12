#' Run the full normalization pipeline
run_pipeline <- function(x, clip = TRUE) {
  y <- zscore(x)
  if (clip) {
    y <- winsorize(y)
  }
  y
}
