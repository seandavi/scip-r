#' An S4 interval
setClass("Interval", representation(lo = "numeric", hi = "numeric"))

setGeneric("width", function(x) standardGeneric("width"))

setMethod("width", "Interval", function(x) x@hi - x@lo)

#' Print method for zscore results
print.zresult <- function(x, ...) {
  cat("zresult of length", length(x), "\n")
  invisible(x)
}

#' A counter
Counter <- R6Class("Counter",
  public = list(
    n = 0,
    add = function(k = 1) {
      self$n <- self$n + k
      invisible(self)
    }
  )
)
