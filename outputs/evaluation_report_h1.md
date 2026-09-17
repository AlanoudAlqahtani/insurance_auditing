# Evaluation report

- Labeled invoices: 913
- Predicted (covered) invoices: 913
- Abstained (no row submitted): 0

## Flagging performance
- Precision: 1.000
- Recall: 1.000
- TP=58  FP=0  FN=0  TN=855

## Category accuracy (set-based, on invoices both sides flagged)
- Mean Jaccard overlap: 0.746
- Exact set match rate: 0.603

## Amount accuracy (on invoices both sides flagged)
- Exact match rate: 0.897
- Mean absolute error: 6316.6 cents

## Calibration (confidence bucket -> accuracy)
flag_accuracy = was the erroneous/correct call right.
row_accuracy  = flag AND category AND expected total all right.

                  n  flag_accuracy  row_accuracy
bucket                                          
[0.00, 0.50)    NaN            NaN           NaN
[0.50, 0.70)    NaN            NaN           NaN
[0.70, 0.85)    NaN            NaN           NaN
[0.85, 0.90)  121.0            1.0      0.942149
[0.90, 0.94)  319.0            1.0      0.971787
[0.94, 1.00]  473.0            1.0      0.976744
