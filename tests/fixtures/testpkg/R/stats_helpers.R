#' Compute z-scores for a numeric vector
zscore <- function(x, na.rm = TRUE) {
  mu <- mean(x, na.rm = na.rm)
  s <- stats::sd(x, na.rm = na.rm)
  (x - mu) / s
}

#' Winsorize a vector at given quantiles
winsorize <- function(x, probs = c(0.05, 0.95)) {
  qs <- stats::quantile(x, probs = probs)
  x[x < qs[1]] <- qs[1]
  x[x > qs[2]] <- qs[2]
  x
}
