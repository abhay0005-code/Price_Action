import pandas as pd
import yfinance as yf

# Define the tickers for Nifty, Bank Nifty, and Sensex
tickers = {"Nifty50": "^NSEI", "BankNifty": "^NSEBANK", "Sensex": "^BSESN"}


# Function to fetch data and write to Excel
def export_indices_data(interval, output_filename):
    with pd.ExcelWriter(output_filename, engine="openpyxl") as writer:
        for sheet_name, ticker in tickers.items():
            # Download intraday data (period="1mo" gets up to 30 days of intraday)
            df = yf.download(ticker, period="1mo", interval=interval, progress=False)

            # Flatten MultiIndex columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Reset index so 'Datetime' becomes a column
            df.reset_index(inplace=True)

            # Standardize column names to lowercase
            df.columns = [col.lower() for col in df.columns]

            # Rename 'datetime' or 'date' to 'timestamp'
            df.rename(columns={"datetime": "timestamp", "date": "timestamp"}, inplace=True)

            # Select and order required columns
            required_cols = ["timestamp", "open", "high", "low", "close", "volume"]
            df = df[required_cols]

            # Convert timezone-aware timestamps to string (Excel compatibility)
            df["timestamp"] = df["timestamp"].astype(str)

            # Export each ticker to a separate worksheet in the same workbook
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"File successfully created: {output_filename}")


# Generate 5-minute interval Excel file
export_indices_data(interval="5m", output_filename="nifty_indices_5min.xlsx")

# Generate 15-minute interval Excel file
export_indices_data(interval="15m", output_filename="nifty_indices_15min.xlsx")