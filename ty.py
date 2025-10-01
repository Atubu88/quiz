def add_numbers(a, b):
    return a + b

def multiply_numbers(a, b):
    return a * b

def main():
    x = 5
    y = 10

    # Добавляем числа
    sum_result = add_numbers(x, y)
    print(f"Sum: {sum_result}")

    # Умножаем числа
    mult_result = multiply_numbers(x, y)
    print(f"Product: {mult_result}")

if __name__ == "__main__":
    main()
