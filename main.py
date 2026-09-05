from core.pipeline import CuriousPipeline


def main():

    print(r"""
           _____ _ _                       _         _
          / ____| (_)                     | |       | |
         | |    | |_ _ __  _ __ ___  _   _| |__   __| | ___
         | |    | | | '_ \\| '__/ _ \\| | | | '_ \\ / _` |/ _ \\
         | |____| | | | | | | | (_) | |_| | | | | (_| |  __/
          \\_____|_|_|_| |_|_|  \\___/ \\__,_|_| |_|\\__,_|\\___|

                     CURIOUS ABOUT THINGS
    """)

    pipeline = CuriousPipeline()

    pipeline.create_episode()


if __name__ == "__main__":

    main()
