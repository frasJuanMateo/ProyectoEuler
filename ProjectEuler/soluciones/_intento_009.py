def solve():
    total = 1000
    for a in range(1, total // 3):
        # b = (total * (total / 2 - a)) / (total - a)
        numerator = total * (total // 2 - a)
        denominator = total - a
        if numerator % denominator == 0:
            b = numerator // denominator
            c = total - a - b
            if a < b < c:
                return a * b * c

if __name__ == "__main__":
    print(solve())
